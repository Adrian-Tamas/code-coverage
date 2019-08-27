# -*- coding: utf-8 -*-

import requests
import structlog
from libmozevent import taskcluster_config
from libmozevent.utils import retry

from code_coverage_events import QUEUE_MONITORING
from code_coverage_events import QUEUE_PULSE

logger = structlog.get_logger(__name__)


class CodeCoverage(object):
    """
    Taskcluster hook handling the code coverage
    """

    def __init__(self, hook_id, hook_group_id, bus):
        self.triggered_groups = set()
        self.group_id = hook_group_id
        self.hook_id = hook_id
        self.bus = bus

        # Setup TC services
        self.queue = taskcluster_config.get_service("queue")
        self.hooks = taskcluster_config.get_service("hooks")

    async def run(self):
        """
        Main consumer, running queued payloads from the pulse listener
        """
        while True:
            # Get next payload from pulse messages
            payload = await self.bus.receive(QUEUE_PULSE)

            # Parse the payload to extract a new task's environment
            envs = self.parse(payload)
            if envs is None:
                continue

            for env in envs:
                # Trigger new tasks
                task = self.hooks.triggerHook(self.group_id, self.hook_id, env)
                task_id = task["status"]["taskId"]
                logger.info("Triggered a new code coverage task", id=task_id)

                # Send task to monitoring
                await self.bus.send(
                    QUEUE_MONITORING, (self.group_id, self.hook_id, task_id)
                )

    def is_coverage_task(self, task):
        return any(
            task["task"]["metadata"]["name"].startswith(s)
            for s in ["build-linux64-ccov", "build-win64-ccov"]
        )

    def get_build_task_in_group(self, group_id):
        if group_id in self.triggered_groups:
            logger.info(
                "Received duplicated groupResolved notification", group=group_id
            )
            return None

        def maybe_trigger(tasks):
            for task in tasks:
                if self.is_coverage_task(task):
                    self.triggered_groups.add(group_id)
                    return task

            return None

        def retrieve_coverage_task(limit=200):
            reply = self.queue.listTaskGroup(group_id, limit=limit)
            task = maybe_trigger(reply["tasks"])

            while task is None and reply.get("continuationToken") is not None:
                reply = self.queue.listTaskGroup(
                    group_id, limit=limit, continuationToken=reply["continuationToken"]
                )
                task = maybe_trigger(reply["tasks"])

            return task

        try:
            return retry(retrieve_coverage_task)
        except requests.exceptions.HTTPError:
            return None

    def parse(self, body):
        """
        Extract revisions from payload
        """
        taskGroupId = body["taskGroupId"]

        build_task = self.get_build_task_in_group(taskGroupId)
        if build_task is None:
            return None

        repository = build_task["task"]["payload"]["env"]["GECKO_HEAD_REPOSITORY"]

        if repository not in [
            "https://hg.mozilla.org/mozilla-central",
            "https://hg.mozilla.org/try",
        ]:
            logger.warn(
                "Received groupResolved notification for a coverage task in an unexpected branch",
                repository=repository,
            )
            return None

        logger.info(
            "Received groupResolved notification for coverage builds",
            repository=repository,
            revision=build_task["task"]["payload"]["env"]["GECKO_HEAD_REV"],
            group=taskGroupId,
        )

        return [
            {
                "REPOSITORY": repository,
                "REVISION": build_task["task"]["payload"]["env"]["GECKO_HEAD_REV"],
            }
        ]
