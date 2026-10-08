"""One-shot arrival reminders using LoomHarbor's existing phone detector."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PresenceReminder(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    person_alias: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    channel: Literal["telegram"]


def presence_arrival_workflow(reminder: PresenceReminder, base_url: str) -> dict:
    def task(name, capability, input_, next_):
        return {
            "id": name,
            "kind": "task",
            "next": next_,
            "action": {
                "mode": "sync",
                "request": {
                    "url": base_url.rstrip("/") + "/trama/execute",
                    "verb": "POST",
                    "headers": {
                        "Authorization": "Bearer __TRAMA_TOKEN__",
                        "Content-Type": "application/json",
                    },
                    "body": {"capability_name": capability, "input": input_},
                },
                "successStatusCodes": [200],
            },
        }

    def read(name, next_):
        return task(
            name,
            "home.execute",
            {
                "capability": "presence.read",
                "target": reminder.person_alias,
                "input": {},
            },
            next_,
        )

    def observed(node, present):
        root = f"nodes.{node}.response.body.output"
        return {
            "and": [
                {"===": [{"var": root + ".success"}, True]},
                {"===": [{"var": root + ".result.present"}, present]},
            ]
        }

    def sleep(name, next_):
        return {"id": name, "kind": "sleep", "durationSeconds": 30, "next": next_}

    return {
        "name": "presence_arrival_reminder",
        "version": "2.0.0",
        # RiteSmith's validator requires this metadata for polling back-edges;
        # Trama does not cap iterations. The terminal path follows notification.
        "max_iterations": 20,
        "entrypoint": "initial_presence",
        "nodes": [
            read("initial_presence", "initial_state"),
            {
                "id": "initial_state",
                "kind": "switch",
                "cases": [
                    {
                        "name": "already_home",
                        "when": observed("initial_presence", True),
                        "target": "read_departure",
                    },
                    {
                        "name": "away",
                        "when": observed("initial_presence", False),
                        "target": "read_arrival",
                    },
                ],
                "default": "retry_initial",
            },
            sleep("retry_initial", "initial_presence"),
            read("read_departure", "check_departure"),
            {
                "id": "check_departure",
                "kind": "switch",
                "cases": [
                    {
                        "name": "left",
                        "when": observed("read_departure", False),
                        "target": "read_arrival",
                    },
                ],
                "default": "wait_departure",
            },
            sleep("wait_departure", "read_departure"),
            read("read_arrival", "check_arrival"),
            {
                "id": "check_arrival",
                "kind": "switch",
                "cases": [
                    {"name": "arrived", "when": observed("read_arrival", True), "target": "notify"},
                ],
                "default": "wait_arrival",
            },
            sleep("wait_arrival", "read_arrival"),
            task("notify", "telegram.send", {"text": reminder.message}, "end"),
        ],
    }
