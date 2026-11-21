"""Readback: what a workflow does, from its graph — the text users are shown."""

import json
from datetime import UTC, datetime

from ritesmith.config import Settings
from ritesmith.core.planning import plan_readback
from ritesmith.core.presence_workflow import PresenceReminder, presence_arrival_workflow
from ritesmith.schemas.artifact import Artifact
from ritesmith.workflows.readback import describe
from ritesmith.workflows.templates import build_template, requested_template

BASE = "http://ritesmith:8081"
NOW = datetime(2026, 11, 21, 14, 7, tzinfo=UTC)  # 11:07 in São Paulo


def recipe(name, params):
    return build_template(*requested_template({"template": {"name": name, "params": params}}), BASE)


def task(node_id, capability, input_, next_="end"):
    body = {"capability_name": capability, "input": input_}
    return {
        "id": node_id,
        "kind": "task",
        "next": next_,
        "action": {"mode": "sync", "request": {"url": f"{BASE}/trama/execute", "body": body}},
    }


def test_the_2026_11_21_misroute_reads_as_an_arrival():
    # The plan that was confirmed as "15/05/2027, every 6 months" must say what it is.
    definition = presence_arrival_workflow(
        PresenceReminder(person_alias="thiago", message="Crédito do Hugo.", channel="telegram"),
        BASE,
    )
    text = describe(definition, start=NOW).text
    assert text.startswith("• Quando thiago chegar em casa: envia no Telegram “Crédito do Hugo.”")
    assert "2027" not in text and "sair" not in text


def test_dated_series_lists_wall_clock_times():
    nodes = []
    gaps = [3600 * 21, 86400 * 7, 86400 * 7]  # 08:07 next day, then weekly
    for i, gap in enumerate(gaps):
        nodes.append({"id": f"wait_{i}", "kind": "sleep", "durationSeconds": gap, "next": f"r_{i}"})
        nodes.append(
            task(
                f"r_{i}", "telegram.send", {"text": "Crédito"}, "end" if i == 2 else f"wait_{i + 1}"
            )
        )
    text = describe({"entrypoint": "wait_0", "nodes": nodes}, start=NOW).text
    assert (
        text
        == "• Envia no Telegram “Crédito” em 22/11/2026 08:07, 29/11/2026 08:07, 06/12/2026 08:07."
    )


def test_threshold_action_names_reading_action_and_deadline():
    text = describe(
        recipe(
            "threshold_action",
            {
                "read": {"capability": "ac.read", "target": "hugo"},
                "metric_path": "state.current_temperature",
                "op": "<",
                "value": 25,
                "action": {
                    "capability": "remote.press",
                    "target": "ventilador_hugo",
                    "input": {"button": "desligar"},
                },
                "notify": "Quarto a {value} °C",
                "interval_seconds": 600,
                "window_seconds": 3600,
            },
        ),
        start=NOW,
    ).text
    assert "hugo state.current_temperature < 25" in text.lower()
    assert (
        "executa remote.press em ventilador_hugo (button=desligar) e depois envia no Telegram"
        in text
    )
    assert "(verifica a cada 10 min)" in text and "Encerra até" in text


def test_presence_change_group_departure_with_delay():
    text = describe(
        recipe(
            "presence_change",
            {
                "people": ["thiago", "jaque"],
                "event": "all_away",
                "delay_seconds": 900,
                "actions": [{"capability": "switch.off", "target": "luz_sala"}],
                "notify": "Casa vazia",
            },
        ),
        start=NOW,
    ).text
    assert text.startswith(
        "• Quando todos (jaque, thiago) tiverem saído de casa, espera 15 min: executa switch.off"
    )


def test_price_variation_distinguishes_base_from_current():
    text = describe(
        recipe("price_variation", {"kind": "crypto", "symbol": "BTC", "percent": 5}), start=NOW
    ).text
    assert "(fetch) price > market.coin_price (base) price × 1.05" in text


def test_split_join_continues_after_the_branches_and_reads_are_not_actions():
    definition = {
        "entrypoint": "reads",
        "nodes": [
            {"id": "reads", "kind": "split", "branches": ["bat", "vac"], "join": "j"},
            task("bat", "home.execute", {"capability": "sensor.battery", "target": "porta"}),
            task("vac", "home.execute", {"capability": "vacuum.state", "target": "x20"}),
            {"id": "j", "kind": "join", "next": "msg"},
            task(
                "msg",
                "telegram.send",
                {"text": "Check-up: {{ nodes.bat.response.body.output.result }}"},
            ),
        ],
    }
    assert describe(definition, start=NOW).text == (
        "• Agora: envia no Telegram “Check-up: ‹valor›”.\n"
        "Consulta: sensor.battery de porta; vacuum.state de x20."
    )


def test_unrolled_repeats_collapse_to_one_line():
    nodes = []
    for i in range(1, 6):
        nxt = f"wait__rs_{i}" if i < 5 else "end"
        nodes += [
            task(f"fetch__rs_{i}", "web.search", {"query": "solar"}, f"check__rs_{i}"),
            {
                "id": f"check__rs_{i}",
                "kind": "switch",
                "default": nxt,
                "cases": [
                    {
                        "name": "new",
                        "target": f"send__rs_{i}",
                        "when": {
                            ">": [{"var": f"nodes.fetch__rs_{i}.response.body.output.count"}, 0]
                        },
                    }
                ],
            },
            task(f"send__rs_{i}", "telegram.send", {"text": "Novidades"}, nxt),
        ]
        if i < 5:
            nodes.append(
                {
                    "id": f"wait__rs_{i}",
                    "kind": "sleep",
                    "durationSeconds": 21600,
                    "next": f"fetch__rs_{i + 1}",
                }
            )
    text = describe({"entrypoint": "fetch__rs_1", "nodes": nodes}, start=NOW).text
    first, sources = text.split("\n")
    assert "até 5 vezes, a cada 6 h (última checagem depois de 1 dia)" in first
    assert sources == "Consulta: web.search (solar)."


def test_plan_readback_skips_non_workflows_and_survives_bad_content():
    def art(kind, content):
        return Artifact.model_validate(
            {
                "artifact_id": "a",
                "artifact_type": kind,
                "name": "n",
                "version": 1,
                "status": "draft",
                "content": content,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )

    wf = presence_arrival_workflow(
        PresenceReminder(person_alias="jaque", message="Oi", channel="telegram"), BASE
    )
    settings = Settings(local_timezone="America/Sao_Paulo")
    text = plan_readback(
        [art("lua_script", "return 1"), art("trama_workflow", json.dumps(wf))], NOW, settings
    )
    assert text.startswith("• Quando jaque chegar em casa")
    assert plan_readback([art("trama_workflow", "{not json")], NOW, settings) is None


def test_polling_shows_when_watching_begins_and_minutes_round():
    definition = recipe(
        "threshold_action",
        {
            "read": {"capability": "presence.read", "target": "sensor_porta"},
            "metric_path": "present",
            "op": "==",
            "value": True,
            "notify": "Movimento",
            "interval_seconds": 60,
            "window_seconds": 3600,
            "start_delay_seconds": 3 * 3600 - 2,  # 13:06:58 → reads as 14:07
        },
    )
    text = describe(definition, start=NOW).text
    assert "(verifica a cada 1 min, a partir de 21/11/2026 14:07)" in text
