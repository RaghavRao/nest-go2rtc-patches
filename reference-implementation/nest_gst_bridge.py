#!/usr/bin/env python3
"""
nest_gst_bridge.py — browser-free Nest (Google SDM) WebRTC receiver on webrtcbin.

Pulls a Nest camera's WebRTC stream server-side and forwards the H264 RTP over UDP
(picked up by mediamtx and republished as RTSP for Frigate). Design notes:
  * offer SDP text is extracted ONCE, inside the create-offer promise callback,
    stored as a plain str; `local-description` is never read (avoids a PyGObject crash);
  * candidates come from `on-ice-candidate` (plain int/str) and are spliced
    into the stored offer text ourselves;
  * all SDM HTTP + set-remote-description run on the GLib main context.

Config via env: NEST_ENV_FILE (path to the .env with FRIGATE_NEST_* creds),
NEST_CAMERA (GARAGE|DOORBELL|PATIO|INDOOR), NEST_UDP_PORT, NEST_TMP. See README.
"""
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

import os, threading, requests, time as _time
from pathlib import Path
from datetime import datetime

def _parse_expiry(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

Gst.init(None)

TMP = Path(os.environ.get("NEST_TMP", "/tmp/nest-bridge"))
TMP.mkdir(parents=True, exist_ok=True)
DUP_CANDS = os.environ.get("DUP_CANDS", "1") == "1"   # duplicate candidates into every m-section (Chrome-shaped)

def log(*a): print(*a, flush=True)

ENV = {}
for l in Path(os.environ.get("NEST_ENV_FILE", "/opt/frigate/.env")).read_text().splitlines():
    t = l.strip()
    if t and not t.startswith("#") and "=" in t:
        k, v = t.split("=", 1); ENV[k] = v
PROJ = ENV["FRIGATE_NEST_DEVICE_ACCESS_PROJECT_ID"]
CAM  = os.environ.get("NEST_CAMERA", "GARAGE").upper()   # GARAGE|DOORBELL|PATIO|INDOOR
DEV  = ENV["FRIGATE_NEST_CAMERA_ID_" + CAM]
UDP_PORT = int(os.environ.get("NEST_UDP_PORT", "5004"))
log(f"[cfg] camera={CAM} udp_port={UDP_PORT} tmp={TMP}")

S = {"offer_text": None, "cands": {}, "sent": False, "vcount": 0, "media_session_id": None}
S_LOCK = threading.Lock()

# ------------------------------------------------------------------ SDM REST

def token():
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": ENV["FRIGATE_NEST_OAUTH_CLIENT_ID"],
        "client_secret": ENV["FRIGATE_NEST_OAUTH_CLIENT_SECRET"],
        "refresh_token": ENV["FRIGATE_NEST_DEVICE_REFRESH_TOKEN"],
        "grant_type": "refresh_token"}, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]

def sdm_exchange(offer_sdp):
    r = requests.post(
        f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{PROJ}/devices/{DEV}:executeCommand",
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        json={"command": "sdm.devices.commands.CameraLiveStream.GenerateWebRtcStream",
              "params": {"offerSdp": offer_sdp}}, timeout=30)
    j = r.json()
    res = j.get("results", {})
    if "answerSdp" not in res:
        log("[sdm] ERROR:", str(j)[:400]); return None
    # note: res["mediaSessionId"] (captured below) is what ExtendWebRtcStream needs later
    S["media_session_id"] = res.get("mediaSessionId")
    S["expiry"] = _parse_expiry(res.get("expiresAt"))
    log("[sdm] mediaSessionId:", S["media_session_id"], "expiresAt:", res.get("expiresAt"))
    return res["answerSdp"]


def extend_stream():
    sid = S.get("media_session_id")
    if not sid:
        return True
    try:
        r = requests.post(
            f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{PROJ}/devices/{DEV}:executeCommand",
            headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
            json={"command": "sdm.devices.commands.CameraLiveStream.ExtendWebRtcStream",
                  "params": {"mediaSessionId": sid}}, timeout=20)
        res = r.json().get("results", {})
        if res.get("mediaSessionId"):
            S["media_session_id"] = res["mediaSessionId"]
        if res.get("expiresAt"):
            S["expiry"] = _parse_expiry(res["expiresAt"])  # wired cams: pushes expiry forward
        log(f"[sdm] extend http={r.status_code} newExpiresAt={res.get('expiresAt')}")
        if r.status_code != 200:
            # battery doorbells/cams can't extend (wired-only) -> recycle timer handles it
            log("[sdm] extend body:", r.text[:200])
    except Exception as e:
        log("[sdm] extend error:", e)
    return True  # keep the timer running

# --------------------------------------------- SDP surgery (FULL port of fixNestSDP)

def fix_nest_sdp(sdp):
    """Five repairs to Google's non-conformant answer SDP: candidate component-id,
    m=application/sctpmap modernization, '/' in msid/ssrc tokens, per-section PT and
    attribute dedup, and empty-line removal. Without these webrtcbin rejects or
    mis-binds the answer. (See REPORT.md Blocker 2 for the per-defect rationale.)"""
    out, seen = [], set()
    for line in sdp.replace("\r\n", "\n").split("\n"):
        if not line:
            continue                                            # (5) no empty lines
        # NOTE: ice-pwd kept VERBATIM (with '=') — patched webrtcbin now accepts it,
        # and the '=' is part of Google's STUN auth key so it must not be stripped.
        if line.startswith(("a=msid:", "a=msid-semantic:", "a=ssrc:")):
            out.append(line.replace("/", "-")); continue        # (3) '/' illegal in tokens
        if line.startswith("m=application"):                    # (2a) deprecated SCTP
            f = line.split()
            out.append(f"m=application {f[1]} UDP/DTLS/SCTP webrtc-datachannel"); continue
        if line.startswith("a=sctpmap:"):                       # (2b)
            out.append("a=sctp-port:" + line[len("a=sctpmap:"):].split()[0]); continue
        if line.startswith("m="):                               # (4) dedupe PTs in m-line
            seen = set()                                        # dedup scope = per section
            f = line.split()
            out.append(" ".join(f[:3] + list(dict.fromkeys(f[3:])))); continue
        if line.startswith("a=candidate:"):                     # (1) malformed candidates
            t = line[len("a=candidate:"):].split()
            if len(t) >= 2 and t[1].lower() in ("udp", "tcp"):
                t = [t[0], "1", *t[1:]]                         # insert missing component-id
            if "generation" in t:
                t = t[:t.index("generation")]                   # strip trailing generation N
            out.append("a=candidate:" + " ".join(t)); continue
        if line.startswith(("a=rtpmap:", "a=fmtp:", "a=rtcp-fb:")):
            if line in seen:
                continue                                        # (4) dedupe attrs per section
            seen.add(line)
        out.append(line)
    return "\r\n".join(out) + "\r\n"

def munge_offer(sdp):
    """Normalize webrtcbin's offer toward Chromium's SDM-accepted shape:
    no port-0/bundle-only, explicit H264 fmtp, standard opus rtpmap, drop
    GStreamer-only attrs SDM may not parse (rtcp-mux-only, rtcp-rsize), add
    msid-semantic + extmap-allow-mixed."""
    out = []
    saw_fmtp96 = any(l.startswith("a=fmtp:96") for l in sdp.splitlines())
    for line in sdp.replace("\r\n", "\n").split("\n"):
        if not line:
            continue
        s = line.strip()
        if s in ("a=bundle-only", "a=rtcp-mux-only", "a=rtcp-rsize"):
            continue                                   # GStreamer-only / SDM-hostile
        if line.startswith("m=video 0 "):
            line = "m=video 9 " + line[len("m=video 0 "):]
        elif line.startswith("m=application 0 "):
            line = "m=application 9 " + line[len("m=application 0 "):]
        elif line.startswith("a=rtpmap:") and "OPUS/48000" in line and "opus/48000/2" not in line:
            line = line.replace("OPUS/48000/2", "opus/48000/2").replace("OPUS/48000", "opus/48000/2")  # RFC opus rtpmap (idempotent)
        out.append(line)
        if line.startswith("a=group:BUNDLE"):
            out.append("a=extmap-allow-mixed")
            out.append("a=msid-semantic: WMS")
        if line.lower().startswith("a=rtpmap:96 h264") and not saw_fmtp96:
            out.append("a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f")
    return "\r\n".join(out) + "\r\n"

def ensure_ice_creds_per_section(sdp):
    """max-bundle offers sometimes carry ice-ufrag/ice-pwd only on the bundle
    master. Chromium repeats them in every section and SDM accepts Chromium —
    so normalize to that."""
    lines = [l for l in sdp.replace("\r\n", "\n").split("\n") if l]
    ufrag = next((l for l in lines if l.startswith("a=ice-ufrag:")), None)
    pwd   = next((l for l in lines if l.startswith("a=ice-pwd:")), None)
    if not ufrag or not pwd:
        return sdp
    out = []
    for i, l in enumerate(lines):
        out.append(l)
        if l.startswith("m="):
            j = i + 1; has = False
            while j < len(lines) and not lines[j].startswith("m="):
                if lines[j].startswith("a=ice-ufrag:"): has = True; break
                j += 1
            if not has:
                out += [ufrag, pwd]
    return "\r\n".join(out) + "\r\n"

def remap_answer_mids(answer, offer):
    """Google's SDM forces mids 0/1/2 in the answer, ignoring our offer's mid
    names -> webrtcbin can't match the answer to its local offer and ICE never
    starts. Rewrite the answer's a=group:BUNDLE + a=mid: (by m-line order) back
    to the offer's mids."""
    offer_mids = [l.split(":", 1)[1].strip()
                  for l in offer.replace("\r\n", "\n").split("\n") if l.startswith("a=mid:")]
    if not offer_mids:
        return answer
    out, idx = [], 0
    for line in answer.replace("\r\n", "\n").split("\n"):
        if line.startswith("a=group:BUNDLE"):
            out.append("a=group:BUNDLE " + " ".join(offer_mids)); continue
        if line.startswith("a=mid:") and idx < len(offer_mids):
            out.append("a=mid:" + offer_mids[idx]); idx += 1; continue
        out.append(line)
    return "\r\n".join(out)


def inject_candidates(offer, cands, duplicate_all=DUP_CANDS):
    """Splice a=candidate: lines into m-sections of the base offer.
    duplicate_all=True reproduces Chromium's known-good shape (one shared
    BUNDLE transport => the same candidate set is valid in every section).
    With max-bundle, on-ice-candidate typically reports mlineindex 0 only."""
    lines = [l for l in offer.replace("\r\n", "\n").split("\n") if l]
    if duplicate_all:
        seen, flat = set(), []
        for ml in sorted(cands):
            for c in cands[ml]:
                if c not in seen:
                    seen.add(c); flat.append(c)
        for_section = lambda sec: flat
    else:
        for_section = lambda sec: list(cands.get(sec, []))
    out, sec = [], -1
    for i, l in enumerate(lines):
        if l.startswith("m="):
            sec += 1
        out.append(l)
        last_in_sec = (i + 1 == len(lines)) or lines[i + 1].startswith("m=")
        if last_in_sec and sec >= 0:
            for c in for_section(sec):
                # GStreamer's candidate already includes the "candidate:" token, so
                # prepend only "a="; and lowercase the transport (Google-shaped).
                cand = c if c.startswith("candidate:") else "candidate:" + c
                parts = cand.split()
                if len(parts) >= 3:
                    parts[2] = parts[2].lower()  # UDP/TCP -> udp/tcp
                out.append("a=" + " ".join(parts))
    return "\r\n".join(out) + "\r\n"

# ------------------------------------------------------------------ GStreamer

wb = None
loop = GLib.MainLoop()

def on_offer_created(promise, _):
    # The ONE place where touching the offer boxed object has proven stable.
    reply = promise.get_reply()
    if reply is None:                       # expired promise — guard or this crashes too
        log("[gst] create-offer promise expired"); loop.quit(); return
    offer = reply.get_value("offer")
    text = offer.sdp.as_text()              # plain-str copy; after this line we're safe
    with S_LOCK:
        S["offer_text"] = text
    (TMP / "gst_offer_base.sdp").write_text(text)
    log(f"[gst] base offer captured ({len(text)}B); set-local-description")
    wb.emit("set-local-description", offer, None)

def on_ice_candidate(el, mlineindex, candidate):
    # libnice thread, but args are plain (int, str) — safe to stash.
    if not candidate or not candidate.startswith("candidate:"):
        return
    with S_LOCK:
        S["cands"].setdefault(mlineindex, []).append(candidate.strip())
    log(f"[gst] cand mline={mlineindex}: {candidate.strip()[:72]}")

def on_gather_state(el, _pspec):
    st = el.get_property("ice-gathering-state")     # enum read is fine
    log("[gst] gathering:", st.value_nick)
    if st == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
        GLib.idle_add(assemble_and_signal)          # hop to main context; NO local-description here

def assemble_and_signal():
    with S_LOCK:
        if S["sent"] or S["offer_text"] is None:
            return False
        S["sent"] = True
        offer = S["offer_text"]
        cands = {k: list(v) for k, v in S["cands"].items()}
    n = sum(len(v) for v in cands.values())
    log(f"[gst] ICE gather complete: {n} candidate(s) on m-lines {sorted(cands)}")
    if not any(" typ srflx" in c for v in cands.values() for c in v):
        log("[gst] WARNING: no srflx candidate — Google's cloud peer cannot reach "
            "bare host candidates. Check UDP egress to stun.l.google.com:19302.")
    final = munge_offer(inject_candidates(ensure_ice_creds_per_section(offer), cands))
    (TMP / "gst_offer_final.sdp").write_text(final)
    for tag in ("transport-wide-cc", "a=rtcp-fb:96 transport-cc"):
        log(f"[gst] offer contains {tag!r}:", tag in final)   # both MUST be true for feedback
    ans = sdm_exchange(final)
    if not ans:
        loop.quit(); return False
    ans = remap_answer_mids(ans, offer)   # Google forces 0/1/2; map back to our offer's mids
    ans = fix_nest_sdp(ans)
    (TMP / "gst_answer_fixed.sdp").write_text(ans)
    res, msg = GstSdp.SDPMessage.new_from_text(ans)
    if res != GstSdp.SDPResult.OK:
        log("[gst] answer parse failed:", res.value_nick); loop.quit(); return False
    answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, msg)
    def _sra(promise, _):
        rep = promise.get_reply()
        log("[gst] set-remote-description reply:", rep.to_string() if rep else "None")
        log("[gst] signaling-state now:", wb.get_property("signaling-state").value_nick)
    wb.emit("set-remote-description", answer, Gst.Promise.new_with_change_func(_sra, None))
    log("[gst] set-remote-description emitted; signaling-state:", wb.get_property("signaling-state").value_nick)
    # Google is non-trickle (ice-lite): its candidates are embedded in the answer SDP.
    # webrtcbin doesn't always extract them -> add each explicitly on bundle m-line 0.
    added = 0
    for line in ans.replace("\r\n", "\n").split("\n"):
        if line.startswith("a=candidate:"):
            wb.emit("add-ice-candidate", 0, line[len("a="):])
            added += 1
    log(f"[gst] added {added} remote ICE candidates on mline 0")
    return False

def trigger_offer():
    log("[gst] create-offer")
    wb.emit("create-offer", None, Gst.Promise.new_with_change_func(on_offer_created, None))
    return False

def fallback_send():
    with S_LOCK:
        pending = not S["sent"] and S["offer_text"] is not None and any(S["cands"].values())
    if pending:
        log("[gst] gather not COMPLETE after 6s; sending with candidates collected so far")
        assemble_and_signal()
    return False

def on_ice_state(el, _p):
    log("[gst] ice-connection-state:", el.get_property("ice-connection-state").value_nick)

def probe_cb(pad, info):
    S["vcount"] += 1
    return Gst.PadProbeReturn.OK

def on_pad_added(el, pad):
    caps = pad.get_current_caps() or pad.query_caps(None)
    s = caps.to_string() if caps else ""
    log("[gst] pad-added:", s[:100])
    if "media=(string)video" in s:
        # Forward the H264 RTP as-is over UDP; ffmpeg (in Frigate) depayloads it and
        # tolerates the transport-cc header extension that crashes GStreamer's depay.
        q = Gst.ElementFactory.make("queue")
        us = Gst.ElementFactory.make("udpsink")
        us.set_property("host", "127.0.0.1"); us.set_property("port", UDP_PORT)
        us.set_property("sync", False); us.set_property("async", False)
        for e in (q, us): pipe.add(e); e.sync_state_with_parent()
        pad.link(q.get_static_pad("sink")); q.link(us)
        pad.add_probe(Gst.PadProbeType.BUFFER, probe_cb)
        log(f"[gst] forwarding VIDEO RTP -> udp://127.0.0.1:{UDP_PORT}")
    else:
        fake = Gst.ElementFactory.make("fakesink")
        pipe.add(fake); fake.sync_state_with_parent()
        pad.link(fake.get_static_pad("sink"))

pipe = Gst.Pipeline.new("p")
wb = Gst.ElementFactory.make("webrtcbin", "wb")
wb.set_property("bundle-policy", "max-bundle")
wb.set_property("stun-server", "stun://stun.l.google.com:19302")
pipe.add(wb)
wb.connect("on-ice-candidate", on_ice_candidate)          # BEFORE set-local-description
wb.connect("pad-added", on_pad_added)
wb.connect("notify::ice-connection-state", on_ice_state)
wb.connect("notify::ice-gathering-state", on_gather_state)

bus = pipe.get_bus(); bus.add_signal_watch()
def on_msg(_b, msg):
    if msg.type == Gst.MessageType.ERROR:
        e, d = msg.parse_error(); log("[gst ERROR]", e.message, "|", d)
    elif msg.type == Gst.MessageType.WARNING:
        e, d = msg.parse_warning(); log("[gst WARN]", e.message)
bus.connect("message", on_msg)

log("[gst] READY:", pipe.set_state(Gst.State.READY).value_nick)

# NOTE: rtcp-fb-* fields in caps are how webrtcbin grows a=rtcp-fb lines in the
# offer. transport-cc is the whole point: it makes the receiver send the TWCC
# feedback Google's sender needs (REPORT.md Blocker 1).
audio_caps = Gst.Caps.from_string(
    "application/x-rtp,media=(string)audio,encoding-name=(string)OPUS,"
    "clock-rate=(int)48000,payload=(int)111")
video_caps = Gst.Caps.from_string(
    'application/x-rtp,media=(string)video,encoding-name=(string)H264,'
    'clock-rate=(int)90000,payload=(int)96,'
    'rtcp-fb-transport-cc=(boolean)true,'
    'rtcp-fb-nack=(boolean)true,'
    'rtcp-fb-nack-pli=(boolean)true,'
    'rtcp-fb-goog-remb=(boolean)true,'
    # transport-cc header extension: puts transport-wide-cc in the offer AND wires
    # rtpsession to read seq numbers on incoming packets -> it emits TWCC feedback.
    'extmap-3=(string)"http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01"')
ta = wb.emit("add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY, audio_caps)
tv = wb.emit("add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY, video_caps)
dc = wb.emit("create-data-channel", "nest", None)
log("[gst] audio_tr:", ta is not None, "video_tr:", tv is not None, "dc:", dc is not None)
log("[gst] PLAYING:", pipe.set_state(Gst.State.PLAYING).value_nick)

GLib.timeout_add(1000, trigger_offer)       # deterministic manual offer (no on-negotiation-needed)
GLib.timeout_add_seconds(180, extend_stream)  # renew SDM stream before ~5min expiry
GLib.timeout_add_seconds(6, fallback_send)
GLib.timeout_add_seconds(5, lambda: (log(f"[gst] video_buffers={S['vcount']}"), True)[1])

# Stall watchdog: if video was flowing then stops (dead/expired SDM session that
# couldn't be extended), exit so systemd Restart=always respawns a fresh session.
S["_last_vcount"] = -1
S["_stalls"] = 0
def stall_watchdog():
    n = S["vcount"]
    if n > 0 and n == S["_last_vcount"]:
        S["_stalls"] += 1
        if S["_stalls"] >= 2:  # ~60s with zero new video buffers
            log(f"[watchdog] video stalled at {n} buffers — exiting for systemd restart")
            os._exit(1)
    else:
        S["_stalls"] = 0
    S["_last_vcount"] = n
    return True
GLib.timeout_add_seconds(30, stall_watchdog)

# Proactive recycle: battery doorbells/cams can't ExtendWebRtcStream, so the ~5min
# session just dies. Exit ~20s BEFORE expiry so systemd respawns a fresh session with
# a minimal gap (~15s reneg) instead of a 60s stall. No-op for wired cams (extend keeps
# pushing S["expiry"] forward, so now never reaches expiry-20).
def recycle_before_expiry():
    exp = S.get("expiry")
    if exp and S["vcount"] > 0 and _time.time() >= exp - 20:
        log("[recycle] session near expiry — exiting for a fresh session")
        os._exit(1)
    return True
GLib.timeout_add_seconds(10, recycle_before_expiry)

try:
    loop.run()
except KeyboardInterrupt:
    pass