# -*- coding: utf-8 -*-
"""
internal.
"""

from __future__ import print_function

from datetime import datetime
import json
import logging
import os
import time

from wandb.filesync.dir_watcher import DirWatcher
from wandb.interface.interface import file_enum_to_policy
from wandb.lib.config import save_config_file_from_dict
from wandb.proto import wandb_internal_pb2  # type: ignore
from wandb.util import sentry_set_scope


# from wandb.stuff import io_wrap

from . import artifacts
from . import file_stream
from . import internal_api
from .file_pusher import FilePusher
from .git_repo import GitRepo


logger = logging.getLogger(__name__)


def _dict_from_proto_list(obj_list):
    d = dict()
    for item in obj_list:
        d[item.key] = json.loads(item.value_json)
    return d


def _config_dict_from_proto_list(obj_list):
    d = dict()
    for item in obj_list:
        d[item.key] = dict(desc=None, value=json.loads(item.value_json))
    return d


class SendManager(object):
    def __init__(self, settings, resp_q, run_meta=None):
        self._settings = settings
        self._resp_q = resp_q
        self._run_meta = run_meta

        self._fs = None
        self._pusher = None
        self._dir_watcher = None

        # is anyone using run_id?
        self._run_id = None

        self._entity = None
        self._project = None

        self._api = internal_api.Api(default_settings=settings)
        self._api_settings = dict()

        # TODO(jhr): do something better, why do we need to send full lines?
        self._partial_output = dict()

        self._exit_code = 0

    def send(self, i):
        t = i.WhichOneof("data")
        if t is None:
            return
        handler = getattr(self, "handle_" + t, None)
        if handler is None:
            print("unknown handle", t)
            return

        # run the handler
        handler(i)

    def _flatten(self, dictionary):
        if type(dictionary) == dict:
            for k, v in list(dictionary.items()):
                if type(v) == dict:
                    self._flatten(v)
                    dictionary.pop(k)
                    for k2, v2 in v.items():
                        dictionary[k + "." + k2] = v2

    def handle_exit(self, data):
        exit = data.exit
        self._exit_code = exit.exit_code

        logger.info("handling exit code: %s", exit.exit_code)

        # Ensure we've at least noticed every file in the run directory. Sometimes
        # we miss things because asynchronously watching filesystems isn't reliable.
        run_dir = self._settings.files_dir
        logger.info("scan: %s", run_dir)

        for dirpath, _, filenames in os.walk(run_dir):
            for fname in filenames:
                file_path = os.path.join(dirpath, fname)
                save_name = os.path.relpath(file_path, run_dir)
                self._save_file(save_name)

        if data.control.req_resp:
            logger.info("informing user process exit was handled")
            # TODO: send something more than an empty result
            resp = wandb_internal_pb2.ResultRecord()
            self._resp_q.put(resp)

    def _maybe_setup_resume(self, run):
        """This maybe queries the backend for a run and fails if the settings are
        incompatible."""
        offsets = {"step": 0, "history": 0, "events": 0, "output": 0, "runtime": 0}
        error = None
        if self._settings.resume:
            # TODO: This causes a race, we need to make the upsert atomically
            # only create or update depending on the resume config
            resume_status = self._api.run_resume_status(
                entity=run.entity, project_name=run.project, name=run.run_id
            )
            if resume_status is None:
                if self._settings.resume == "must":
                    error = wandb_internal_pb2.ErrorData()
                    error.code = wandb_internal_pb2.ErrorData.ErrorCode.INVALID
                    error.message = (
                        "resume='must' but run (%s) doesn't exist" % run.run_id
                    )
            else:
                if self._settings.resume == "never":
                    error = wandb_internal_pb2.ErrorData()
                    error.code = wandb_internal_pb2.ErrorData.ErrorCode.INVALID
                    error.message = "resume='never' but run (%s) exists" % run.run_id
                elif self._settings.resume in ("allow", "auto"):
                    try:
                        history = json.loads(
                            json.loads(resume_status["historyTail"])[-1]
                        )
                    except (IndexError, ValueError):
                        history = {}
                    # TODO: Do we need to restore config / summary?
                    offsets["runtime"] = history.get("_runtime", 0)
                    offsets["step"] = history.get("_step", 0)
                    offsets["history"] = resume_status["historyLineCount"]
                    offsets["events"] = resume_status["eventsLineCount"]
                    offsets["output"] = resume_status["logLineCount"]
        return offsets, error

    def handle_run(self, data):
        run = data.run
        run_tags = run.tags[:]

        # build config dict
        config_dict = None
        if run.HasField("config"):
            config_dict = _config_dict_from_proto_list(run.config.update)
            config_path = os.path.join(self._settings.files_dir, "config.yaml")
            save_config_file_from_dict(config_path, config_dict)

        repo = GitRepo(remote=self._settings.git_remote)

        offsets, error = self._maybe_setup_resume(run)
        if error is not None:
            if data.control.req_resp:
                resp = wandb_internal_pb2.ResultRecord()
                resp.run_result.run.CopyFrom(run)
                resp.run_result.error.CopyFrom(error)
                self._resp_q.put(resp)
            else:
                logger.error("Got error in async mode: %s", error.message)
            return

        ups, inserted = self._api.upsert_run(
            name=run.run_id,
            entity=run.entity or None,
            project=run.project or None,
            group=run.run_group or None,
            job_type=run.job_type or None,
            display_name=run.display_name or None,
            notes=run.notes or None,
            tags=run_tags or None,
            config=config_dict or None,
            sweep_name=run.sweep_id or None,
            host=run.host or None,
            program_path=self._settings.program or None,
            repo=repo.remote_url,
            commit=repo.last_commit,
        )

        # TODO: not checking `inserted` for now

        start_time = run.start_time.ToSeconds() - offsets["runtime"]
        if data.control.req_resp:
            resp = wandb_internal_pb2.ResultRecord()
            resp.run_result.run.CopyFrom(run)
            resp_run = resp.run_result.run
            resp_run.starting_step = offsets["step"]
            # TODO: is this really what we want?
            resp_run.start_time.FromSeconds(start_time)
            storage_id = ups.get("id")
            if storage_id:
                resp_run.storage_id = storage_id
            display_name = ups.get("displayName")
            if display_name:
                resp_run.display_name = display_name
            project = ups.get("project")
            if project:
                project_name = project.get("name")
                if project_name:
                    resp_run.project = project_name
                    self._project = project_name
                entity = project.get("entity")
                if entity:
                    entity_name = entity.get("name")
                    if entity_name:
                        resp_run.entity = entity_name
                        self._entity = entity_name
            self._resp_q.put(resp)

        if self._entity is not None:
            self._api_settings["entity"] = self._entity
        if self._project is not None:
            self._api_settings["project"] = self._project
        self._fs = file_stream.FileStreamApi(
            self._api, run.run_id, start_time, settings=self._api_settings
        )
        # Ensure the streaming polices have the proper offsets
        self._fs.set_file_policy("wandb-summary.json", file_stream.SummaryFilePolicy())
        self._fs.set_file_policy(
            "wandb-history.jsonl",
            file_stream.JsonlFilePolicy(start_chunk_id=offsets["history"]),
        )
        self._fs.set_file_policy(
            "wandb-events.jsonl",
            file_stream.JsonlFilePolicy(start_chunk_id=offsets["events"]),
        )
        self._fs.set_file_policy(
            "output.log",
            file_stream.CRDedupeFilePolicy(start_chunk_id=offsets["output"]),
        )
        self._fs.start()
        self._pusher = FilePusher(self._api)
        self._dir_watcher = DirWatcher(self._settings, self._api, self._pusher)
        self._run_id = run.run_id
        if self._run_meta:
            self._run_meta.write()
        sentry_set_scope("internal", run.entity, run.project)
        logger.info("run started: %s", self._run_id)

    def handle_history(self, data):
        history = data.history
        history_dict = _dict_from_proto_list(history.item)
        if self._fs:
            # print("about to send", d)
            self._fs.push("wandb-history.jsonl", json.dumps(history_dict))
            # print("got", x)

    def handle_summary(self, data):
        summary = data.summary
        summary_dict = _dict_from_proto_list(summary.update)
        json_summary = json.dumps(summary_dict)
        if self._fs:
            self._fs.push("wandb-summary.json", json_summary)
        summary_path = os.path.join(self._settings.files_dir, "wandb-summary.json")
        with open(summary_path, "w") as f:
            f.write(json_summary)

    def handle_stats(self, data):
        stats = data.stats
        if stats.stats_type != wandb_internal_pb2.StatsData.StatsType.SYSTEM:
            return
        if not self._fs:
            return
        now = stats.timestamp.seconds
        d = dict()
        for item in stats.item:
            d[item.key] = json.loads(item.value_json)
        row = dict(system=d)
        self._flatten(row)
        row["_wandb"] = True
        row["_timestamp"] = now
        # TODO: run has a start_time, as well as history and settings :(
        row["_runtime"] = int(now - self._settings._start_time)
        self._fs.push("wandb-events.jsonl", json.dumps(row))
        # TODO(jhr): check fs.push results?

    def handle_output(self, data):
        if not self._fs:
            return
        out = data.output
        prepend = ""
        stream = "stdout"
        if out.output_type == wandb_internal_pb2.OutputData.OutputType.STDERR:
            stream = "stderr"
            prepend = "ERROR "
        line = out.line
        if not line.endswith("\n"):
            self._partial_output.setdefault(stream, "")
            self._partial_output[stream] += line
            # TODO(jhr): how do we make sure this gets flushed?
            # we might need this for other stuff like telemetry
        else:
            # TODO(jhr): use time from timestamp proto
            # TODO(jhr): do we need to make sure we write full lines?
            # seems to be some issues with line breaks
            cur_time = time.time()
            timestamp = datetime.utcfromtimestamp(cur_time).isoformat() + " "
            prev_str = self._partial_output.get(stream, "")
            line = u"{}{}{}{}".format(prepend, timestamp, prev_str, line)
            self._fs.push("output.log", line)
            self._partial_output[stream] = ""

    def handle_config(self, data):
        cfg = data.config
        config_dict = _config_dict_from_proto_list(cfg.update)
        self._api.upsert_run(
            name=self._run_id, config=config_dict, **self._api_settings
        )
        config_path = os.path.join(self._settings.files_dir, "config.yaml")
        save_config_file_from_dict(config_path, config_dict)
        # TODO(jhr): check result of upsert_run?

    def _save_file(self, fname, policy="end"):
        logger.info("saving file %s with policy %s", fname, policy)
        self._dir_watcher.update_policy(fname, policy)

    def handle_files(self, data):
        files = data.files
        for k in files.files:
            # TODO(jhr): fix paths with directories
            self._save_file(k.path, file_enum_to_policy(k.policy))

    def handle_artifact(self, data):
        artifact = data.artifact
        saver = artifacts.ArtifactSaver(
            api=self._api,
            digest=artifact.digest,
            manifest_json=artifacts._manifest_json_from_proto(artifact.manifest),
            file_pusher=self._pusher,
            is_user_created=artifact.user_created,
        )

        saver.save(
            type=artifact.type,
            name=artifact.name,
            metadata=artifact.metadata,
            description=artifact.description,
            aliases=artifact.aliases,
            use_after_commit=artifact.use_after_commit,
        )

    def finish(self):
        logger.info("shutting down sender")
        if self._dir_watcher:
            self._dir_watcher.finish()
        if self._pusher:
            self._pusher.finish()
        if self._fs:
            # TODO(jhr): now is a good time to output pending output lines
            self._fs.finish(self._exit_code)
        if self._pusher:
            self._pusher.print_status()
