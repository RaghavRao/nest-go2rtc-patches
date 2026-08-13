# Reference receiver: Nest WebRTC → UDP RTP → mediamtx RTSP

A working server-side receiver that pulls a Nest camera's SDM WebRTC stream, sends the
TWCC feedback Google needs (Blocker 1), applies the SDP repairs (Blocker 2), and forwards
H264 RTP over UDP. mediamtx republishes it as RTSP for Frigate. This exists to show the
fix set is **sufficient** — the upstream goal is to reproduce the behavior inside go2rtc/pion.

## Layout

```
nest_gst_bridge.py            the receiver (patched-webrtcbin client)
mediamtx.yml                  RTSP server config (per-camera runOnDemand -> ffmpeg -> RTSP)
sdp/garage.sdp                SDP mediamtx feeds to ffmpeg for the UDP RTP (one per camera)
env/garage.conf.example       per-camera unit env (NEST_UDP_PORT); copy to env/<cam>.conf
systemd/nest-mediamtx.service systemd --user unit for mediamtx
systemd/nest-bridge@.service  systemd --user template unit (one instance per camera)
```

The systemd units and `mediamtx.yml` assume an install root of `/opt/frigate/nest-bridge/`
laid out as `bin/mediamtx`, `gst/plugins/libgstwebrtc.so` (the renamed patched plugin, which
the bridge unit puts on `GST_PLUGIN_PATH`), plus `sdp/` and `env/`. Install there or edit
the paths in the units and `mediamtx.yml`.

## The patched GStreamer plugin

The receiver needs `webrtcbin` with the ICE-charset patch (`../patches/gstreamer/webrtcsdp-ice-pwd.patch`).

- **Prebuilt (fast path — x86-64, Fedora 44, GStreamer 1.28.6):** GStreamer only scans
  files named `*.so`, so copy the prebuilt plugin into a directory **as `libgstwebrtc.so`**
  and put that dir first on `GST_PLUGIN_PATH` (so it overrides the system plugin):
  ```bash
  mkdir -p ~/nest-gst/plugins
  cp ../patches/gstreamer/libgstwebrtc.so.patched ~/nest-gst/plugins/libgstwebrtc.so
  export GST_PLUGIN_PATH=~/nest-gst/plugins:$GST_PLUGIN_PATH
  ```
  (Built against gst-plugins-bad 1.28.6, tested 2026-08 — must match your GStreamer core.)
- **Build it yourself (any other platform/version):**
  ```bash
  # deps (Fedora names): gcc meson ninja-build gstreamer1-devel \
  #   gstreamer1-plugins-base-devel libnice-devel libsrtp-devel openssl-devel glib2-devel
  curl -LO https://gstreamer.freedesktop.org/src/gst-plugins-bad/gst-plugins-bad-<your-version>.tar.xz
  tar xf gst-plugins-bad-*.tar.xz && cd gst-plugins-bad-*
  git apply -p1 ../patches/gstreamer/webrtcsdp-ice-pwd.patch   # or patch -p1 <
  meson setup build -Dwebrtc=enabled && ninja -C build ext/webrtc/libgstwebrtc.so
  # then point GST_PLUGIN_PATH at build/ext/webrtc (+ build/gst-libs if you built those)
  ```
  Match the source version to your installed GStreamer core, or the plugin won't load.

## Configure

1. Put your Google SDM credentials in a `.env` (mode 600, kept OUT of any repo) with:
   `FRIGATE_NEST_DEVICE_ACCESS_PROJECT_ID`, `FRIGATE_NEST_OAUTH_CLIENT_ID`,
   `FRIGATE_NEST_OAUTH_CLIENT_SECRET`, `FRIGATE_NEST_DEVICE_REFRESH_TOKEN`,
   `FRIGATE_NEST_CAMERA_ID_<CAM>` (one per camera). Point the bridge at it with
   `NEST_ENV_FILE=/path/to/.env` (defaults to `/opt/frigate/.env`).
2. `cp env/garage.conf.example env/garage.conf` and set `NEST_UDP_PORT` (unique per camera).
3. Each camera needs a matching `sdp/<cam>.sdp` (copy `sdp/garage.sdp`, set the `m=video`
   port to the same `NEST_UDP_PORT`) and a `path` in `mediamtx.yml`.

## Run

```bash
# manual (one camera). mediamtx: download a static binary from
# github.com/bluenviron/mediamtx/releases (used unmodified). GST_PLUGIN_PATH points at the
# renamed patched plugin from the step above.
export GST_PLUGIN_PATH=~/nest-gst/plugins:$GST_PLUGIN_PATH
export NEST_CAMERA=garage NEST_UDP_PORT=5004 NEST_ENV_FILE=/opt/frigate/.env
./mediamtx mediamtx.yml &                       # RTSP on :18554
python3 nest_gst_bridge.py                      # forwards to udp:5004; mediamtx serves /garage
# Frigate then reads rtsp://host.containers.internal:18554/garage
```

Or install the systemd `--user` units (they hand off lifecycle to systemd; a stall watchdog
+ pre-expiry recycle keep battery cameras cycling):
```bash
systemctl --user enable --now nest-mediamtx.service
systemctl --user enable --now nest-bridge@garage.service
```

## Notes

- **Battery cameras** can't `ExtendWebRtcStream` (Blocker 4) — the bridge recycles the
  session ~20s before expiry, so expect a short reconnect gap each cycle.
- **Security:** `mediamtx.yml` binds `:18554` on all interfaces so a containerized Frigate
  can reach it; the RTSP paths are unauthenticated — firewall it on an untrusted LAN.
- **Known limitation:** the stall watchdog only fires once video has started (`vcount>0`);
  a session that never starts (e.g. a bad refresh token) is not auto-restarted — check the
  logs on first bring-up.
