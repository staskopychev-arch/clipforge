"""Offline image smoke test: FFmpeg + sandboxed headed Chrome, no publication."""
import subprocess
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from playwright.sync_api import sync_playwright
from app.services.browser_network import isolated_youtube_browser
from app.services.media import probe_duration, render_vertical_clip


def main():
    assert os.getuid() == 10001, "Browser smoke must run as the application user"
    process_status = Path("/proc/self/status").read_text()
    assert "CapEff:\t0000000000000000" in process_status, "Runtime capabilities must be dropped"
    assert "NoNewPrivs:\t1" in process_status and "Seccomp:\t2" in process_status
    with TemporaryDirectory(prefix="clipforge-smoke-") as folder:
        source, clip = Path(folder) / "source.mp4", Path(folder) / "clip.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=640x360:r=30", "-f", "lavfi", "-i", "sine=frequency=440",
                        "-t", "6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
                       check=True, timeout=60)
        render_vertical_clip(source, clip, 0, 5)
        assert 4.9 <= probe_duration(clip) <= 5.2
    with sync_playwright() as driver:
        with isolated_youtube_browser(driver, headless=False) as browser:
            context = browser.new_context()
            page = context.new_page()
            page.set_content("<h1>ClipForge smoke OK</h1>")
            assert page.locator("h1").inner_text() == "ClipForge smoke OK"
            page.goto("chrome://sandbox")
            sandbox_status = " ".join(page.locator("body").inner_text().split())
            print(sandbox_status, flush=True)
            normalized = sandbox_status.casefold()
            namespace_active = ("namespace sandbox yes" in normalized or
                                ("layer 1 sandbox namespace" in normalized and "pid namespaces yes" in normalized))
            assert namespace_active and "network namespaces yes" in normalized and "seccomp-bpf sandbox yes" in normalized
            print("Chromium sandbox verified: namespaces and Seccomp-BPF enabled")
            context.close()
    print("Headed browser and media smoke passed; no external upload was performed")


if __name__ == "__main__":
    main()
