import assert from "node:assert/strict";
import test from "node:test";
import { api } from "../src/api.ts";

test("repair dialog accepts the FastAPI repository list and absent collections", async (t) => {
  const fetch = t.mock.method(globalThis, "fetch");
  const repo = { name: "demo", fullName: "team/demo" };
  for (const [body, names] of [
    [[repo], ["team/demo"]],
    [{ repos: [{ name: "team/demo" }] }, ["team/demo"]],
    [[], []],
    [{}, []],
    [null, []],
  ]) {
    fetch.mock.mockImplementation(async () => Response.json(body));
    assert.deepEqual((await api.ghRepos()).repos.map((r) => r.name), names);
  }
});

test("newly queued run can render before its timeline arrives", async (t) => {
  const run = { id: "queued-1", repo: "team/demo", status: "working" };
  t.mock.method(globalThis, "fetch", async (path, options) => {
    if (path === "/api/runs" && options.method !== "POST") return Response.json({ runs: [run] });
    return Response.json(run);
  });
  for (const result of [
    await api.createRun({ repo: run.repo, branch: "main", workflowRun: "", context: "" }),
    (await api.runs()).runs[0],
    await api.run(run.id),
  ]) {
    assert.equal(result.id, run.id);
    assert.deepEqual(result.steps, []);
  }
});
