# Nest (SDM WebRTC) → go2rtc / Frigate: upstream fix package

Google Nest 2021+ cameras are **WebRTC-only** (SDM API, no RTSP). go2rtc's `nest:` source
connects, but the video is **unstable** — it freezes, stutters, and drops (see Frigate
discussion [#17527](https://github.com/blakeblackshear/frigate/discussions/17527)). This
package documents what go2rtc / Frigate must change to support these cameras reliably, and
ships a **working reference receiver** as proof the fix set is sufficient.

## Read this first

- **[`REPORT.md`](REPORT.md)** — the full analysis: four blockers, root causes, and the
  concrete change each of go2rtc and Frigate needs.

The four blockers, shortest version:

1. **No transport-cc feedback** → Google starves the video (freeze/stutter). *(the hard one)*
2. **Non-conformant answer SDP** → strict parsers drop ICE candidates → no connection.
3. **ICE creds use base64 `=`/`-`/`_`** → RFC 8839-strict validators reject them.
4. **Battery devices can't `ExtendWebRtcStream`** → must regenerate the stream every ~5 min.

## What's here

| Path | What it is |
|---|---|
| `REPORT.md` | The upstream report (start here). |
| `patches/go2rtc/` | Instrumented, prototyped SDP-repair patch for `pkg/nest/client.go` (Blocker 2; a subset of the repairs). Fixing the SDP let ICE progress but didn't alone sustain video — corroborating that Blocker 1 is separate. |
| `patches/gstreamer/` | `webrtcbin` ICE-cred validator patch (unified diff, Blocker 3) plus the prebuilt plugin `libgstwebrtc.so.patched`. |
| `reference-implementation/` | A **working** receiver (patched `webrtcbin` → UDP RTP → mediamtx RTSP) with TWCC feedback, the SDP surgery, and battery-session recycling. **Build + run steps in [its README](reference-implementation/README.md).** |
| `evidence/` | A sanitized Google answer SDP showing the non-conformance (Blocker 2). |

## Notes

- The reference bridge reads its Google OAuth credentials from a local `.env` at runtime
  (`FRIGATE_NEST_*` variables) — configure it there; nothing is hard-coded.
- **`libgstwebrtc.so.patched`** is a build of `gst-plugins-bad` 1.28.6 on Fedora 44
  (x86-64) (verified live 2026-08). For other platforms/versions, rebuild from
  `webrtcsdp-ice-pwd.patch` — steps in [reference-implementation/README](reference-implementation/README.md).
- The reference receiver is GStreamer, **not** go2rtc — it exists to prove that
  TWCC feedback + the SDP repairs are jointly sufficient. The upstream task is to
  reproduce that behavior inside go2rtc/pion.

## License & attribution

- This repo's own files: see [LICENSE](LICENSE).
- `patches/go2rtc/client.go.patched` derives from **go2rtc (MIT)** — attribution to
  AlexxIT/go2rtc; use under go2rtc's license.
- `patches/gstreamer/webrtcsdp-ice-pwd.patch` and `libgstwebrtc.so.patched` derive from
  GStreamer `gst-plugins-bad` (**LGPL-2.1+**); the `.so` is provided under the LGPL, with
  corresponding source available via the patch + build steps above.

## Related

- Frigate community guide & troubleshooting: [discussion #17527](https://github.com/blakeblackshear/frigate/discussions/17527)
- go2rtc Nest "400 Bad Request" issue: [#1097](https://github.com/AlexxIT/go2rtc/issues/1097)
- Home Assistant Nest streaming issue: [#159547](https://github.com/home-assistant/core/issues/159547)
