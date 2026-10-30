"""BE-SCRIPT: ScriptService storage — CRUD, unique names, example protection and idempotent seeding.
Runs on SQLite and (with NEXUS_TEST_PG_URL) on PostgreSQL."""
from __future__ import annotations

import asyncio

import pytest

from app.scripting import examples as examples_mod
from app.scripting.service import MSG_EXAMPLE_PROTECTED, ScriptService, ScriptServiceError, normalize_name

CODE = 'indicator("Mi media", overlay=True)\nplot(ta.sma(close, 20), "SMA 20")\n'


def run(coro):
    return asyncio.run(coro)


async def _service(rt) -> ScriptService:
    svc = ScriptService(rt)
    await svc.start()
    return svc


def _expect(status: int, coro_factory):
    async def inner():
        with pytest.raises(ScriptServiceError) as info:
            await coro_factory()
        assert info.value.status_code == status
        return info.value.detail

    return inner()


def test_normalize_name_ignores_case_accents_and_spaces():
    assert normalize_name("  Cruce   de EMÁS ") == normalize_name("cruce de emas")
    assert normalize_name("RSI") != normalize_name("RSI 2")


def test_seeds_examples_and_status(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = await _service(rt)
            metas = await svc.list_scripts()
            expected = [name for _, name, _ in examples_mod.EXAMPLES]
            assert len(expected) >= 9
            assert sorted(m["name"] for m in metas) == sorted(expected)
            assert all(m["is_example"] is True for m in metas)
            st = svc.status()
            assert st["running"] is True and st["examples"] == len(expected) and st["scripts"] == len(expected)
            assert {"running", "scripts", "examples", "runs", "last_run_ms"} <= set(st)
            full = await svc.get(metas[0]["id"])
            assert set(full) == {"id", "name", "description", "is_example", "updated_at", "code", "created_at"}
            assert full["code"].strip() and full["description"]
            await svc.stop()
            assert svc.status()["running"] is False

    run(scenario())


def test_seeding_is_idempotent_and_refreshes_changed_examples(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = await _service(rt)
            before = {m["name"]: m["id"] for m in await svc.list_scripts()}
            report = await svc.seed_examples()
            assert report["inserted"] == 0 and report["updated"] == 0 and report["removed"] == 0
            assert {m["name"]: m["id"] for m in await svc.list_scripts()} == before

            # Stored code drifted (older app version) -> refreshed in place, same id
            name = examples_mod.EXAMPLES[0][1]
            await rt.db.execute("UPDATE scripts SET code = ? WHERE name = ?", ("# viejo", name))
            report = await svc.seed_examples()
            assert report["updated"] == 1
            refreshed = await svc.get(before[name])
            assert refreshed["code"] == examples_mod.load_examples()[0]["code"]

            # An example that no longer ships is removed; user scripts are never touched
            await rt.db.execute(
                "INSERT INTO scripts(name, description, code, is_example, created_at, updated_at) VALUES (?, ?, ?, 1, 1, 1)",
                ("Ejemplo retirado", "", "# x"),
            )
            mine = await svc.create("Mi script", CODE)
            report = await svc.seed_examples()
            assert report["removed"] == 1
            names = {m["name"] for m in await svc.list_scripts()}
            assert "Ejemplo retirado" not in names and "Mi script" in names
            assert (await svc.get(mine["id"]))["code"] == CODE

    run(scenario())


def test_user_script_with_an_example_name_is_preserved(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            name = examples_mod.EXAMPLES[1][1]
            await rt.db.execute(
                "INSERT INTO scripts(name, description, code, is_example, created_at, updated_at) VALUES (?, ?, ?, 0, 1, 1)",
                (name, "mío", CODE),
            )
            svc = await _service(rt)
            metas = {m["name"]: m for m in await svc.list_scripts()}
            assert metas[name]["is_example"] is False
            assert (await svc.get(metas[name]["id"]))["code"] == CODE
            assert metas[f"{name} (ejemplo)"]["is_example"] is True
            again = await svc.seed_examples()
            assert again["inserted"] == 0 and again["updated"] == 0

    run(scenario())


def test_crud_unique_names_and_validation(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = await _service(rt)
            created = await svc.create("  Mi   RSI  ", CODE, "Prueba")
            assert created["name"] == "Mi RSI" and created["is_example"] is False
            assert created["description"] == "Prueba" and created["code"] == CODE
            assert created["created_at"] > 0 and created["updated_at"] >= created["created_at"]
            assert svc.status()["scripts"] == len(examples_mod.EXAMPLES) + 1

            detail = await _expect(409, lambda: svc.create("mi rsi", CODE))
            assert "Ya existe" in detail
            await _expect(409, lambda: svc.create(examples_mod.EXAMPLES[0][1].upper(), CODE))
            await _expect(400, lambda: svc.create("   ", CODE))
            await _expect(400, lambda: svc.create("Vacío", "   \n "))
            await _expect(400, lambda: svc.create("x" * 200, CODE))
            await _expect(400, lambda: svc.create("Grande", "#" * 100_001))

            other = await svc.create("Otro", CODE)
            await _expect(409, lambda: svc.update(other["id"], name="MI RSI"))
            same = await svc.update(created["id"], name="MI RSI")  # renaming itself (case) is fine
            assert same["name"] == "MI RSI"
            updated = await svc.update(created["id"], code="# nuevo\n" + CODE, description="Nueva")
            assert updated["code"].startswith("# nuevo") and updated["description"] == "Nueva"
            assert updated["updated_at"] >= created["updated_at"]
            assert (await svc.update(created["id"]))["code"] == updated["code"]  # nothing to change

            assert await svc.delete(other["id"]) == {"ok": True}
            await _expect(404, lambda: svc.get(other["id"]))
            await _expect(404, lambda: svc.delete(other["id"]))
            await _expect(404, lambda: svc.get("abc"))
            await _expect(404, lambda: svc.get(10**20))

    run(scenario())


def test_examples_are_read_only(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = await _service(rt)
            example = (await svc.list_scripts())[0]
            assert await _expect(403, lambda: svc.update(example["id"], code="# hack")) == MSG_EXAMPLE_PROTECTED
            assert await _expect(403, lambda: svc.delete(example["id"])) == MSG_EXAMPLE_PROTECTED
            assert (await svc.get(example["id"]))["code"] != "# hack"
            copy = await svc.create(f"{example['name']} copia", (await svc.get(example["id"]))["code"])
            assert copy["is_example"] is False

    run(scenario())


def test_match_and_find(runtime_factory):
    async def scenario():
        async with runtime_factory() as rt:
            svc = await _service(rt)
            ema = next(m for m in await svc.list_scripts() if m["name"].startswith("Cruce de EMAs"))
            assert (await svc.find("cruce de emas"))["id"] == ema["id"]  # unique prefix
            assert (await svc.find(str(ema["id"])))["id"] == ema["id"]  # by id
            assert (await svc.find("ICHIMOKU"))["name"] == "Ichimoku completo"  # substring, any case
            await svc.create("Prueba A", CODE)
            await svc.create("Prueba B", CODE)
            detail = await _expect(400, lambda: svc.find("prueba"))
            assert "ambiguo" in detail
            assert (await svc.find("prueba a"))["name"] == "Prueba A"  # exact beats prefix
            await _expect(404, lambda: svc.find("no existe nada así"))
            assert await svc.match("") == []

    run(scenario())
