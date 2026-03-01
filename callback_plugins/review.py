# Minimal stdout callback for review/audit playbooks.
# Shows only debug message content and errors — suppresses task headers,
# ok/skipped noise, and set_fact/uri internals.
#
# Usage:
#   ANSIBLE_STDOUT_CALLBACK=review ansible-playbook review_pve.yaml

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = """
    name: review
    type: stdout
    short_description: Clean output for review playbooks
    description:
        - Shows play names as section headers
        - Prints debug msg content verbatim (no task name or ok prefix)
        - Shows failures and unreachable hosts
        - Suppresses all other output (task headers, ok, skipped, set_fact)
        - Intended for read-only audit playbooks where debug output IS the report
    requirements:
        - set as stdout callback in config or via ANSIBLE_STDOUT_CALLBACK=review
"""

from ansible import constants as C
from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "review"

    def v2_playbook_on_play_start(self, play):
        name = play.get_name().strip()
        if name:
            self._display.display(f"\n{'=' * 60}", color=C.COLOR_HIGHLIGHT)
            self._display.display(f"  {name}", color=C.COLOR_HIGHLIGHT)
            self._display.display(f"{'=' * 60}", color=C.COLOR_HIGHLIGHT)

    def v2_playbook_on_task_start(self, task, is_conditional):
        pass

    def v2_runner_on_ok(self, result):
        action = result._task.action
        if action in ("ansible.builtin.debug", "debug"):
            msg = result._result.get("msg", "")
            if isinstance(msg, list):
                for line in msg:
                    self._display.display(str(line))
            else:
                self._display.display(str(msg))

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if ignore_errors:
            return
        task_name = result._task.get_name()
        msg = result._result.get("msg", result._result.get("stderr", ""))
        host = result._host.get_name()
        self._display.display(
            f"FAILED [{host}] {task_name}: {msg}", color=C.COLOR_ERROR
        )

    def v2_runner_on_skipped(self, result):
        pass

    def v2_runner_on_unreachable(self, result):
        host = result._host.get_name()
        msg = result._result.get("msg", "")
        self._display.display(f"UNREACHABLE [{host}]: {msg}", color=C.COLOR_ERROR)

    def v2_playbook_on_stats(self, stats):
        self._display.display("")
        hosts = sorted(stats.processed.keys())
        for host in hosts:
            summary = stats.summarize(host)
            parts = []
            for key in ("ok", "changed", "unreachable", "failures", "skipped"):
                parts.append(f"{key}={summary.get(key, 0)}")
            line = f"{host} : {' '.join(parts)}"
            if summary.get("failures", 0) or summary.get("unreachable", 0):
                self._display.display(line, color=C.COLOR_ERROR)
            else:
                self._display.display(line, color=C.COLOR_OK)

    def v2_playbook_on_include(self, included_file):
        pass

    def v2_playbook_on_no_hosts_matched(self):
        self._display.display("No hosts matched", color=C.COLOR_WARN)

    def v2_playbook_on_no_hosts_remaining(self):
        self._display.display("No more hosts remaining", color=C.COLOR_ERROR)
