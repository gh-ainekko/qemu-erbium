#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Real Chromium + self-hosted GP/WASM IDE + real sysemu firmware UART.

Owns only an isolated server/emulator process group. Never attaches to or resets
the live port 8000, a desktop, noVNC, or a physical serial device.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

from ide_integration import ROOT, generate_project
from web_ide_driver import assert_clean_browser_gp_log, browser_driver, verify_wire


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def asset_hashes(assets):
    return {str(p.relative_to(assets)): sha(p)
            for p in sorted(assets.rglob("*")) if p.is_file()}


def gp_string(text):
    return "'" + text.replace("'", "''") + "'"


def get_status(base):
    with urllib.request.urlopen(base + "/api/status", timeout=2) as response:
        return json.load(response)


def run(args):
    from playwright.sync_api import sync_playwright

    assert args.port != 8000, "refusing live IDE port 8000"
    # Binding preflight prevents a stale/live server from being treated as ours.
    with socket.socket() as probe:
        # Like aiohttp's listener: allow immediate reruns despite TIME_WAIT.
        # This does not permit another active listener (no SO_REUSEPORT).
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", args.port))
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    assert not (out / "result.json").exists(), "use a fresh --output directory"
    generate_project(out / "large.ubp")
    driver = browser_driver()
    (out / "browser-driver.gp").write_text(driver)
    base = f"http://127.0.0.1:{args.port}"
    command = [args.server_python, str(ROOT / "smallvm/web/server.py"),
               "--repo", str(ROOT), "--web", str(ROOT / "smallvm/web"),
               "--assets", str(args.assets.resolve()), "--logs", str(out / "server"),
               "--port", str(args.port), "--elf", str(args.elf.resolve()),
               "--emu", str(args.emu.resolve())]
    browser_logs, browser_errors, failed_requests, asset_errors = [], [], [], []
    requests, ws_urls = [], []
    started = time.monotonic()
    result = {"status": "FAIL", "server_command": command}
    server = browser = context = page = pw = None
    status_before = None
    elf_digest, emulator_digest = sha(args.elf), sha(args.emu)
    assets_before = asset_hashes(args.assets)
    provenance_path = args.assets / "provenance.json"
    provenance_before = json.loads(provenance_path.read_text()) if provenance_path.exists() else None
    frontend_before = {str(p.relative_to(ROOT)): sha(p)
                       for p in (ROOT / "smallvm/web").iterdir()
                       if p.is_file() and p.name in
                       ("index.html", "erbium.js", "bridge.gp", "server.py", "build.py")}
    try:
        wasm = args.assets / "gp_wasm.wasm"
        assert wasm.stat().st_size > 100000 and wasm.read_bytes()[:8] == b"\0asm\1\0\0\0", (
            "real GP interpreter missing from WASM; rebuild browser assets")
        assert provenance_before, "missing browser build provenance"
        for name, digest in provenance_before["overlays_sha256"].items():
            assert sha(ROOT / "smallvm/web" / name) == digest, (
                f"browser build provenance is stale for {name}; rebuild/refresh assets")
        with (out / "server.log").open("wb") as log:
            server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert server.poll() is None, "server exited; inspect server.log"
            try:
                status_before = get_status(base)
                break
            except OSError:
                time.sleep(.1)
        assert status_before and status_before["emulator_running"], "firmware did not start"
        assert status_before["firmware_sha256"] == elf_digest
        assert status_before["emulator_sha256"] == emulator_digest
        pw = sync_playwright().start()
        if pw:
            browser = pw.chromium.launch(headless=not args.headed,
                                         args=["--disable-dev-shm-usage"])
            result["browser_version"] = browser.version
            context = browser.new_context(viewport={"width": 1280, "height": 900},
                                          accept_downloads=True)
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.on("console", lambda msg: browser_logs.append(f"{msg.type}: {msg.text}"))
            page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
            page.on("requestfailed", lambda req: failed_requests.append(
                {"url": req.url, "failure": req.failure}))
            page.on("request", lambda req: requests.append({"url": req.url, "type": req.resource_type}))
            page.on("response", lambda response: asset_errors.append(
                {"url": response.url, "status": response.status})
                if response.status >= 400 else None)
            page.on("websocket", lambda ws: ws_urls.append(ws.url))
            page.add_init_script("""window.__webIdeMessages = [];
                window.addEventListener('message', event => {
                    if (event.source === window && Array.isArray(event.data) &&
                        String(event.data[0]).startsWith('web-ide-')) {
                        window.__webIdeMessages.push(event.data);
                    }
                });""")
            response = page.goto(base + "/", wait_until="load", timeout=120000)
            assert response.status == 200
            page.wait_for_function("window.ErbiumIDE && !document.querySelector('#erbium-connect').disabled",
                                   timeout=120000)
            page.locator(".app-preloader").wait_for(state="hidden", timeout=30000)
            # Real canvas, real categories and startup. A toolbar-only shell fails.
            assert page.locator("iframe").count() == 0, "remote desktop iframe is not a web IDE"
            canvas = page.locator("#canvas")
            assert canvas.is_visible()
            pixel_probe = """() => {
                const c = document.querySelector('#canvas');
                const data = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                const colors = new Set();
                for (let i=0; i<data.length; i+=64) colors.add(Array.from(data.slice(i,i+4)).join(','));
                return {width:c.width, height:c.height, colors:colors.size};
            }"""
            page.wait_for_function("(" + pixel_probe + ")().colors > 8", timeout=10000)
            pixels = page.evaluate(pixel_probe)
            assert pixels["width"] > 300 and pixels["height"] > 300 and pixels["colors"] > 8, pixels
            assert page.locator('[data-ide="categories-list"]').inner_text().strip()
            page.screenshot(path=str(out / "startup.png"))
            # Exercise the application's real browser FileReader/drop/import route.
            page.locator("#FileUploader").set_input_files(str(out / "large.ubp"))
            page.wait_for_function("""async () => (await ErbiumIDE.source()).includes('UART-STRESS-23:')""",
                                   timeout=30000)
            page.screenshot(path=str(out / "large-project.png"))
            imported_source = page.evaluate("() => ErbiumIDE.source()")
            (out / "imported.ubp").write_text(imported_source)
            page.locator("#erbium-connect").click()
            page.wait_for_function("""async () => {
                const s=await ErbiumIDE.runtimeState();
                return s.connected && !s.connecting && s.vmVersion >= 300;
            }""", timeout=30000)
            result["runtime_state_initial"] = page.evaluate("() => ErbiumIDE.runtimeState()")
            page.screenshot(path=str(out / "connected.png"))
            # Install *test functions only* in the actual running top-level module.
            # loadModuleFromString would reset that module's variable dictionary.
            # Add only functions and clear dispatch caches; preserve all GUI state.
            load = ("loadFunctions (topLevelModule) (parse " + gp_string(driver)
                    + ")\nclearMethodCache\ntrue")
            page.evaluate("(source) => ErbiumIDE.evalGP(source)", load)
            config = json.dumps({"edited": (out / "edited.ubp").read_text(), "port": "webserial"})
            page.evaluate("(source) => ErbiumIDE.evalGP(source)",
                          "setGlobal 'ideTestConfig' (jsonParse " + gp_string(config) + ")")
            # Execute assertions on the same main GP API stack as real UI
            # operations, not a launched child task that steals UART replies.
            deadline = time.monotonic() + args.timeout
            result["main_api_stages"] = []

            def stage(name):
                remaining = deadline - time.monotonic()
                assert remaining > 0, "browser GP integration deadline exceeded"
                began = time.monotonic()
                value = page.evaluate(
                    "({source, timeout}) => ErbiumIDE.evalGP(source, timeout)",
                    {"source": name, "timeout": max(1000, int(remaining * 1000))})
                assert value is True, f"GP stage did not complete: {name}: {value!r}"
                result["main_api_stages"].append(
                    {"name": name, "elapsed_seconds": round(time.monotonic() - began, 3)})

            stage("webIdePrepare")
            page.locator("button.--run").click()
            stage("webIdeObserveRunning")
            page.screenshot(path=str(out / "running.png"))
            page.locator("button.--stop").click()
            stage("webIdeObserveStopped")
            page.locator("#erbium-disconnect").click()
            page.wait_for_function("!document.querySelector('#erbium-connect').disabled", timeout=10000)
            page.locator("#erbium-connect").click()
            page.wait_for_function("""async () => {
                const s = await ErbiumIDE.runtimeState();
                return s.connected && !s.connecting && s.vmVersion >= 300;
            }""", timeout=30000)
            result["runtime_state_reconnected"] = page.evaluate("() => ErbiumIDE.runtimeState()")
            stage("webIdeRoundtrip")
            page.wait_for_function(
                "window.__webIdeMessages.some(m => m[0] === 'web-ide-result')", timeout=10000)
            messages = page.evaluate("window.__webIdeMessages")
            reports = [json.loads(m[1]) for m in messages if m[0] == "web-ide-result"]
            assert len(reports) == 1, "missing/duplicate GP result"
            result.update(reports[0])
            result["gp_status"] = reports[0]["status"]
            (out / "gp-result.json").write_text(json.dumps(reports[0], indent=2) + "\n")
            assert result["status"] == "PASS", reports[0]
            result["browser_start_stop_controls_exercised"] = True
            result["browser_reconnect_control_exercised"] = True
            for event, filename in (
                ("web-ide-readback", "readback.json"),
                ("web-ide-original-source", "original.ubp"),
                ("web-ide-decompiled-source", "decompiled.ubp"),
            ):
                artifacts = [m[1] for m in messages if m[0] == event]
                assert len(artifacts) == 1, f"missing/duplicate {event}"
                (out / filename).write_text(artifacts[0])
            page.wait_for_function("ErbiumIDE.transportState().state === 'disconnected'",
                                   timeout=10000)
            outgoing = (out / "server/uart-client-to-emulator.bin").read_bytes()
            incoming = (out / "server/uart-emulator-to-client.bin").read_bytes()
            result.update(verify_wire(outgoing, incoming, result,
                                      json.loads((out / "readback.json").read_text())))
            result["uart_bytes"] = {"ide-to-board": len(outgoing), "board-to-ide": len(incoming)}
            page.screenshot(path=str(out / "roundtrip.png"))
            # Save the actual recovered project through the real Project menu.
            # This is a browser download, not writeFile or a mocked picker.
            page.locator("button.--project").click()
            with page.expect_download(timeout=30000) as pending:
                page.get_by_text("Save", exact=True).click()
            pending.value.save_as(str(out / "saved.ubp"))
            assert (out / "saved.ubp").read_text() == page.evaluate("() => ErbiumIDE.source()")
            result["browser_save_download_exercised"] = True
            result["transport"] = page.evaluate("() => ErbiumIDE.transportState()")
            assert result["transport"]["connections"] == 2, result["transport"]
            assert not result["transport"]["lastError"], result["transport"]
            result["backend_status"] = get_status(base)
            assert result["backend_status"]["emulator_pid"] == status_before["emulator_pid"]
            assert result["backend_status"]["emulator_restarts"] == 0
            assert not browser_errors, browser_errors
            assert not [line for line in browser_logs if line.startswith("error:")], browser_logs[-20:]
            assert not asset_errors, asset_errors
            assert not failed_requests, failed_requests
            assert_clean_browser_gp_log(browser_logs)
            assert not any("novnc" in r["url"].lower() or "websockify" in r["url"].lower()
                           for r in requests), "remote desktop transport observed"
            assert len(ws_urls) == 2 and all(url == base.replace("http:", "ws:") + "/uart"
                                            for url in ws_urls), ws_urls
            result["gui_canvas"] = pixels
            result["gp_error_log_checks"] = "PASS"
            result["real_browser_gui"] = True
            context.tracing.stop(path=str(out / "trace.zip"))
            browser.close()
            browser = context = page = None
        outgoing = (out / "server/uart-client-to-emulator.bin").read_bytes()
        incoming = (out / "server/uart-emulator-to-client.bin").read_bytes()
        result.update(verify_wire(outgoing, incoming, result,
                                  json.loads((out / "readback.json").read_text())))
        assert assets_before == asset_hashes(args.assets), "browser assets changed during test"
        assert all(sha(ROOT / name) == digest for name, digest in frontend_before.items()), (
            "web application source changed during test")
        assert sha(args.elf) == elf_digest and sha(args.emu) == emulator_digest, (
            "firmware/emulator binary changed during test")
        result["assets_unchanged_during_run"] = True
        result["uart_bytes"] = {"ide-to-board": len(outgoing), "board-to-ide": len(incoming)}
        result["status"] = "PASS"
    except Exception as exc:
        result.update(status="FAIL", failure=f"{type(exc).__name__}: {exc}")
        if page:
            try:
                page.screenshot(path=str(out / "failure.png"))
                state = page.evaluate("window.__webIdeMessages")
                (out / "failure-state.json").write_text(json.dumps(state, indent=2))
                reports = [json.loads(m[1]) for m in state if m[0] == "web-ide-result"]
                if reports:
                    result["gp_status"] = reports[-1]["status"]
                    if reports[-1].get("failure"):
                        result["gp_failure"] = reports[-1]["failure"]
                    (out / "gp-result.json").write_text(json.dumps(reports[-1], indent=2) + "\n")
            except Exception:
                pass
        if context:
            try:
                context.tracing.stop(path=str(out / "trace.zip"))
            except Exception:
                pass
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if pw:
            pw.stop()
        if server and server.poll() is None:
            os.killpg(server.pid, signal.SIGTERM)
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait()
        (out / "browser.log").write_text("\n".join(browser_logs) + "\n")
        (out / "browser-network.json").write_text(json.dumps(
            {"requests": requests, "failed": failed_requests, "errors": asset_errors,
             "page_errors": browser_errors, "websockets": ws_urls}, indent=2) + "\n")
        source_paths = [ROOT / "smallvm/tests/ide_integration.gp",
                        ROOT / "smallvm/tests/ide_integration.py",
                        ROOT / "scripts/test-smallvm-web.sh"]
        source_paths += list((ROOT / "smallvm/tests").glob("web_ide*"))
        source_paths += list((ROOT / "smallvm/web").glob("*.py"))
        source_paths += list((ROOT / "smallvm/web").glob("*.gp"))
        source_paths += list((ROOT / "smallvm/web").glob("*.js"))
        source_paths += list((ROOT / "smallvm/web").glob("*.html"))
        source_paths += [p for p in (ROOT / "smallvm").rglob("*")
                         if p.is_file() and p.suffix in (".c", ".h", ".S", ".ld", ".patch")]
        source_paths += [ROOT / "smallvm/Makefile"]
        result.update(elapsed_seconds=round(time.monotonic() - started, 3),
                      elf_sha256=elf_digest, emulator_sha256=emulator_digest,
                      driver_sha256=sha(out / "browser-driver.gp"),
                      project_sha256=sha(out / "large.ubp"),
                      sources_sha256={str(p.relative_to(ROOT)): sha(p)
                                      for p in sorted(set(source_paths)) if p.is_file()})
        result["browser_assets_sha256"] = assets_before
        result["frontend_at_start_sha256"] = frontend_before
        result["smallvm_tree"] = subprocess.check_output(
            ["git", "-C", str(ROOT / "ext/smallvm"), "rev-parse", "HEAD^{tree}"], text=True).strip()
        result["browser_build_provenance"] = provenance_before
        result["repository_commit"] = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ("sources_sha256", "browser_assets_sha256")}, indent=2))
    return 0 if result["status"] == "PASS" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--assets", type=Path, default=ROOT / "build/smallvm/web")
    parser.add_argument("--elf", type=Path, default=ROOT / "build/smallvm/smallvm.elf")
    parser.add_argument("--emu", type=Path, default=ROOT / "dist/bin/erbium_emu")
    parser.add_argument("--server-python", default="/usr/bin/python3")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "build/smallvm/web-tests" / time.strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    args.assets = args.assets.resolve()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
