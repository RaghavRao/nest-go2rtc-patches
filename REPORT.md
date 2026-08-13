# What go2rtc / Frigate need to natively ingest Google Nest (SDM WebRTC) cameras

**Status:** solved out-of-tree with a patched GStreamer `webrtcbin` receiver. This report
says what upstream would change so the patch is not needed.

---

## 1. TL;DR

Nest 2021+ cameras (doorbell + cams) speak **WebRTC only** via Google's Smart Device
Management (SDM) API — `CameraLiveStream.supportedProtocols = ['WEB_RTC']`, no RTSP. A
server-side WebRTC client must pull the stream and re-expose it (e.g. as RTSP) for Frigate.

go2rtc already has a `nest:` source, and the community has it *partly* working — the
canonical guide is Frigate discussion
[#17527](https://github.com/blakeblackshear/frigate/discussions/17527). But the streams are
**unstable**: freezing, stutters, corrupted frames, drops. The community has fixed the
*renewal* half of the problem (see §2); what remains is a receive-side feedback gap that
keeps the video freezing. There are
**four independent blockers**, in descending difficulty:

1. **Congestion-control feedback (the hard one).** Google's sender starves the video track
   to zero within seconds unless the receiver sends transport-wide-cc (TWCC) RTCP feedback.
   go2rtc/pion negotiates the session but does not deliver effective feedback → **video
   `recv_bytes=0`, audio survives** (the diagnostic signature).
2. **Non-conformant answer SDP.** Google's SDP answer breaks strict parsers (ICE candidates
   missing the component-id, deprecated SCTP syntax, illegal `/` in tokens, duplicate
   payload types, forced mids). Candidates get dropped → ICE never connects.
3. **ICE credential charset.** Google's `ice-ufrag`/`ice-pwd` are base64 with `=`,`-`,`_`,
   which RFC 8839-strict validators reject.
4. **Battery-device session lifecycle (API constraint, not a bug).** `ExtendWebRtcStream`
   is wired-only; battery doorbells/cams must regenerate the stream every ~5 min.

Blockers 2–4 are straightforward. Blocker 1 is the real work.

---

## 2. Evidence

- **go2rtc `nest:` source is unstable, not dead.** The community
  ([#17527](https://github.com/blakeblackshear/frigate/discussions/17527)) gets *degraded*
  video — "short video stutters", "reconnecting every 5-10 minutes", "corrupted frames",
  "No Frames have been received", streams dying after minutes. In our own setup the video
  producer showed `recv_bytes=0` with only a brief audio burst (a more complete form of the
  same failure). Related: go2rtc [#1097](https://github.com/AlexxIT/go2rtc/issues/1097), HA
  [#159547](https://github.com/home-assistant/core/issues/159547).
- **The community already fixed the RENEWAL half** (prior art, credited): @PrutsMeneer's
  go2rtc fork keeps the renewal loop alive across a failed renewal and handles 401 token
  expiry ("no reconnects" after a week); @bober10113 transcodes via ffmpeg and finds that
  *disabling event detection* cuts session drops. What those fixes do **not** cure is the
  freezing — because renewal is not the feedback problem in Blocker 1.
- **Proven working receiver:** a patched GStreamer `webrtcbin` client that (a) sends TWCC
  feedback and (b) applies the SDP repairs below **sustains video** — thousands of RTP
  packets at ~100+/s, indefinitely (with session recycling for battery devices). See
  `reference-implementation/nest_gst_bridge.py`.
- **The blockers are independent.** We patched go2rtc's `pkg/nest/client.go` to repair the
  answer SDP (`patches/go2rtc/`); that fixed the SDP and let ICE progress, but did not by
  itself deliver sustained video — consistent with Blocker 1 being separate from Blocker 2.
  We did not fully instrument go2rtc's post-fix byte counts, so treat this as corroboration,
  not proof; the clean proof that TWCC feedback is *sufficient* is the GStreamer receiver
  above. Confirming Blocker 1 inside go2rtc is the open verification (§3).

---

## 3. Blocker 1 — Receive-side congestion-control feedback (go2rtc / pion)

### Why it happens
Google runs full Google Congestion Control (GCC) on the sender. The sender's bandwidth
estimate depends on **transport-wide congestion control** RTCP feedback
(`draft-holmer-rmcat-transport-wide-cc-extensions-01`) from the receiver, optionally
`goog-remb`. With no feedback, the estimate collapses and the sender paces the video track
to zero. Audio is a few kbps and is not GCC-gated the same way, so **audio survives while
video dies** — this is the signature to look for.

This is the residual instability [#17527](https://github.com/blakeblackshear/frigate/discussions/17527)
never resolves. The renewal forks (§2) stop the 5-minute *disconnects*, but the picture
still *freezes* — because pacing-to-zero is a feedback problem, not a session-lifetime one.
The thread treats the symptoms (renewal, transcode, disable detection) and reaches
"moderate success", but doesn't reach the underlying congestion-control mechanism; this
report tries to supply it.

This is **not** a Chrome-vs-Chromium gate: plain open-source Chromium works, because it runs
libwebrtc's GCC + feedback (`modules/congestion_controller/goog_cc/`,
`modules/remote_bitrate_estimator/`, BSD-licensed).

### What go2rtc/pion needs to change
pion **has** the machinery — `github.com/pion/interceptor/pkg/twcc.NewSenderInterceptor`
generates the receiver-side TWCC feedback, and `MediaEngine.RegisterHeaderExtension` (with the
holmer transport-wide-cc URI) negotiates the extension. The open question to confirm is whether
go2rtc's minimal MediaEngine / interceptor registry for the Nest recvonly path registers and
emits it — start by checking whether go2rtc's current offer even advertises the transport-cc
extmap + `rtcp-fb`. Concretely:

1. In the **offer**, on the recvonly video m-section, negotiate:
   - the header extension
     `a=extmap:<n> http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01`
   - `a=rtcp-fb:<pt> transport-cc` (and, defensively, `goog-remb`, `nack`, `nack pli`).
2. Register the TWCC feedback generator so pion **actually emits** transport-cc RTCP for
   received packets (not just advertises support).
3. Verify with a packet capture that periodic `transport-cc` RTCP leaves the receiver, and
   that the incoming video bitrate then holds instead of decaying toward the codec's floor.

The reference receiver shows the exact caps/extmap that make a sender keep feeding (see
`reference-implementation/nest_gst_bridge.py`, the `rtcp-fb-transport-cc` + `extmap-3`
video caps). Reproducing that feedback behavior in pion is the fix.

> **Confidence:** the audio-survives/video-starves signature plus the working TWCC-enabled
> receiver make this the strong diagnosis. The precise pion wiring depends on the go2rtc/pion
> version in tree; the definitive check is a packet capture showing transport-cc RTCP leaving
> the receiver and the video bitrate holding — worth capturing before/after any patch.

---

## 4. Blocker 2 — Repair Google's non-conformant answer SDP (go2rtc)

Google's SDM answer must be rewritten before a strict stack (pion, webrtcbin) will use it.
Five repairs, each with its failure mode. (Reference: `fix_nest_sdp` in the bridge; a
prototyped go2rtc version is in `patches/go2rtc/nest-sdp-fix.patch`.)

| # | Defect in Google's answer | Symptom | Repair |
|---|---|---|---|
| 1 | ICE candidate lines miss the **component-id** (and add a stray leading space / trailing `generation N`) | parser drops all candidates → ICE never connects | insert `1` as component-id; strip `generation N` |
| 2 | Deprecated SCTP: `m=application … DTLS/SCTP` + `a=sctpmap:` | data channel section rejected/mis-bound | modernize to `UDP/DTLS/SCTP webrtc-datachannel` + `a=sctp-port:` |
| 3 | Illegal `/` in `a=msid:`/`a=ssrc:`/`a=msid-semantic:` tokens | token parse error | replace `/` with `-` |
| 4 | Duplicate payload types in the `m=` line, duplicate `rtpmap`/`fmtp`/`rtcp-fb` | mis-binding / parse error | dedup per section |
| 5 | Stray empty lines | some parsers choke | drop empty lines |

**Plus a sixth, mid remapping:** Google **forces `a=mid:0/1/2`** in the answer, ignoring the
offer's mid names. A receiver that matches answer sections to its local offer by mid then
can't associate them and ICE never starts. Rewrite the answer's `a=group:BUNDLE` and
`a=mid:` (by m-line order) back to the offer's mids. See `remap_answer_mids`.

Some senders also want the **offer** shaped like Chromium's (standard `opus/48000/2`
rtpmap, `extmap-allow-mixed`, explicit H264 `fmtp`, no GStreamer-only `rtcp-mux-only`/
`rtcp-rsize`, ice creds repeated per section). See `munge_offer` / `ensure_ice_creds_per_section`.

---

## 5. Blocker 3 — ICE credential charset (RFC 8839 strictness)

Google's `ice-ufrag`/`ice-pwd` are base64 and contain `=`, `-`, `_`. RFC 8839's `ice-char`
grammar is stricter, so a strict validator rejects the remote answer. GStreamer's
`webrtcbin` did exactly this in `webrtcsdp.c: _validate_ice_attr` (see
`patches/gstreamer/webrtcsdp-ice-pwd.patch`). pion happened to be lenient here.

**Fix:** accept the base64 charset for remote ICE creds (or don't over-validate remote
values). **Do not strip the `=`** — it is part of Google's STUN short-term-credential key;
removing it breaks integrity checks and ICE fails at connectivity checks.

---

## 6. Blocker 4 — Battery-device session lifecycle (API constraint)

Per Google's docs, `ExtendWebRtcStream` is available for **wired cameras only**. Battery
doorbells/cams return `400 FAILED_PRECONDITION` on extend; the session simply expires at
~5 min and must be **regenerated** (`GenerateWebRtcStream` again). Confirmed live: our
wired garage cam extends (HTTP 200); the battery doorbell 400s on extend.

**What native support needs:** per device (or on extend-400), **recycle** the stream —
tear down and regenerate shortly before expiry — instead of trying to extend. Expect a
short (~10–15 s) reconnect gap each cycle on battery devices.

This also answers the **battery-drain** question
[#17527](https://github.com/blakeblackshear/frigate/discussions/17527) raises but never
resolves: a 24/7 server-side pull keeps a battery doorbell streaming continuously and
flattens it (confirmed live — ours reached "battery low, camera off"). The fix is
event-driven capture — stream only when the doorbell's own motion/person/press event
fires — not continuous ingest. Refs:
[doorbell-battery](https://developers.google.com/nest/device-access/api/doorbell-battery),
[CameraLiveStream trait](https://developers.google.com/nest/device-access/traits/device/camera-live-stream).

---

## 7. What Frigate specifically needs

Frigate embeds go2rtc, so **fixing go2rtc fixes Frigate's `nest:` path**. Frigate-side items:

- **Inherit the go2rtc fix.** Once go2rtc delivers feedback + repairs SDP + recycles
  battery sessions, Frigate's `nest:` source works with no Frigate code change.
- **Document the model reality:** Nest 2021+ cams are WebRTC-only and on-demand; streams are
  ~5-min sessions (battery = cycling with brief gaps). Users should not expect gapless 24/7
  from battery devices.
- **Reconnect tolerance (already OK):** Frigate's detect ffmpeg already retries on input
  loss; the only nicety is treating the periodic battery-cycle gap as expected, not as a
  camera fault (avoid noisy error logs / false "camera down").

---

## 8. The ask

Concretely, what would help from maintainers:

- **go2rtc:** confirm whether the Nest recvonly path negotiates and *emits* transport-cc
  feedback today; if not, wire the TWCC sender interceptor (Blocker 1). The SDP-repair patch
  here (Blocker 2) is ready to adapt. Happy to open a scoped issue/PR and capture the
  before/after packet trace.
- **Frigate:** no code change needed beyond inheriting the go2rtc fix; documenting the
  WebRTC-only / on-demand / battery-cycling reality would save others the debugging.

This report and the reference receiver are a starting point, not a finished patch — feedback
on the diagnosis is welcome.

## 9. Files in this repo

```
patches/go2rtc/         prototyped SDP-repair patch for pkg/nest/client.go — instrumented,
                        implements a subset of the repairs pion needs (Blocker 2)
patches/gstreamer/      ice-pwd validator patch (real unified diff) + built plugin (Blocker 3)
reference-implementation/  a WORKING receiver showing the full fix set is sufficient, with
                        build/run steps in its own README
evidence/               sanitized sample answer SDP showing Google's non-conformance
```

The reference receiver is GStreamer, not go2rtc — it exists to prove that TWCC feedback +
the SDP repairs are **jointly sufficient** to sustain Nest video server-side. The upstream
work is to reproduce that behavior inside go2rtc/pion.
