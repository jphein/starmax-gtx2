# GTX2 watch-face INSTALL — transfer protocol + native container format

How a custom watch face actually reaches the GTX2 over BLE, and the exact on-wire container
the watch consumes. **Fully capture-derived** — reverse-engineered byte-exact from our own
BLE capture of the Runmefit app installing a dial. Nothing here comes from the APK/SDK, so it
is PORTABLE (safe to inform the Gadgetbridge coordinator).

> This resolves the open question left by `docs/watchface-format.md` §3.3/§5.2 ("delivery is
> blocked on one missing capture"). That census predated the capture used here. The
> distributed dial **container** (a ZIP of `dial.json` + PNG/BMP) is covered by
> `watchface-format.md`; **this** doc covers the transcoded form + the transfer.

Implemented by `starmax_client/dialfmt.py` (container codec) and
`starmax_client/commands/dials.py` (the `dial-push` CLI command + streaming driver), with
byte-exact regression tests in `tests/test_dials.py`.

> **PORTABLE — candidate for the Gadgetbridge coordinator.** Because this protocol + container
> format are `[CAP]` (derived only from our BLE captures, never the APK/SDK), they are safe to
> port into the Gadgetbridge `starmax` device support to give Gadgetbridge **custom watch-face
> install**. The transport (bulk plane) and the native-container codec map directly onto a
> future `installDial()` there; only the CLI/bleak glue is standalone-specific.

## Evidence

Source: the app pushing a market dial (`CWR05G_23687`) as a custom face over the
command-write characteristic (ATT handle `0x0026`). Two independent checksums confirm the
reconstruction is exact:

* the 989 reassembled `D2` chunks total exactly the declared **231 293 bytes**;
* the `D4` finalize's whole-file **CRC-16/XMODEM** equals `crc16_xmodem(container)`;
* the container's own header word at `0x28` equals `zlib.crc32(container[0x2c:])`.

## 1. The install is a bulk-plane push — and it auto-activates

A dial install uses the **same `D1/D2/D3/D4` bulk plane** as firmware (`res.ota`) and AGPS
(`ephemeris.gnss`) — see `docs/firmware-dfu.md §B`. There is **no** separate C1
announce/stream sub-protocol. The dial-specific parts are only the **filename** and that the
install **auto-activates**.

```
(optional)  read dial-list        C1 0x16, f1=0      pre-check installed set + active face
   D3        d3 00                                    resume/state probe
             <- d3 00 00 <u32 staged_off> <u32 f2>    (staged_off 0 = fresh)
   D1        d1 00 <u32 size> <u32 size> 0f custom_id_<ID>.bin 00   announce (field2 == size)
             <- d1 00 00                               announce accepted
   D2 x N    d2 <ctr> <=234B chunk>                   stream; ctr wraps 0..255
             <- d2 00 00 <u32 cum_off> <u32 run_crc>  windowed progress, ~every 15 chunks
   D4        d4 00 00 <u32 crc16/xmodem>              finalize (whole-container CRC)
             <- d4 00 00                               verify OK -> install + AUTO-ACTIVATE
(optional)  read dial-list        C1 0x16, f1=0      confirm: entry count +1, active = new face
```

* **Filename convention:** the D1 announce carries `custom_id_<dialId>.bin` (observed
  `custom_id_25022.bin`). `<dialId>` is the id the watch keys the face by; it is *independent*
  of the container's internal `name`.
* **Auto-activate (verified):** the `0x16` reply's active-dial field flips from the previous
  face to `custom_id_<ID>.bin` immediately on `D4`, with entry count +1 and storage grown by
  the manifest's declared `size` (not the byte length). No switch command is sent — switching
  to an *already-installed* face is a separate, still-uncaptured operation.
* **Flow control:** the watch acks with `d2 00 00 <cum_offset> <running_crc16>` about every 15
  chunks; `running_crc16` is `crc16_xmodem(container[0:cum_offset])`. **Reliable delivery is
  required:** our client sends the D-plane frames **write-WITH-response** — live-verified,
  fire-hosing write-without-response overran the watch (it stopped acking a 231 KB push at
  ~84 KB, so the finalize CRC rejected the truncated blob). Awaiting each ATT
  write-with-response paces the stream to what the watch absorbs. (The phone app's own stack
  paces its write-without-response bursts; we can't rely on that from bleak.)

## 2. The on-wire payload is a TRANSCODED native container (not the ZIP)

The distributed dial `.bin` is a ZIP (`watchface-format.md`). The app does **not** stream that
ZIP — it transcodes the ZIP's `firmware/` subtree into a flat native container and streams
that. The captured payload contains no `PK`/PNG/BMP magic. Layout (little-endian unless noted):

| offset | size | field | notes |
|---|---|---|---|
| `0x00` | 30 | `name` | dial internal name, NUL-padded ASCII |
| `0x1e` | u16 | `MAGIC1` | `0x4321` |
| `0x20` | u16 | `MAGIC2` | `0x5AA5` |
| `0x22` | 2 | `CONST_A` | `06 04` (constant in the one sample; preserved opaque) |
| `0x24` | u16 | `count` | number of asset entries — **big-endian** |
| `0x26` | 2 | `CONST_B` | `00 04` (constant; == `dial_version`?) |
| `0x28` | u32 | `crc32` | `zlib.crc32(container[0x2c:])` |
| `0x2c` | 38×count | asset table | each `char name[30]` + `u32 offset` + `u32 length` (absolute) |
| … | | asset data | payloads, contiguous, table order, zero padding |

`starmax_client.dialfmt.parse_blob()` / `build_blob()` reproduce the captured 231 293-byte
container **byte-for-byte** (round-trip and build-from-scratch both verified identical).

### 2.1 Per-asset encoding — SOLVED (byte-exact)
Cracked by pulling the dial's **source ZIP from the CDN** and comparing it to the captured
container. Each asset:

* `dial.json` / `file.json`: embedded **verbatim UTF-8**.
* image assets: `<type:1> <(height<<13)|(width<<2) : u24 LE> <lz4.block payload>`
  * `type 0x18` = **RGBA8888** (uncompressed = W·H·4, channel order R,G,B,A)
  * `type 0x04` = **RGB565 little-endian** (uncompressed = W·H·2)
  * payload = one raw **`lz4.block`** stream (no stored size); uncompressed size = W·H·bpp.

Verified: the decoder reproduces the **source pixels of all 22 image assets** exactly
(`lz4.block` decompress at offset 4 == the decoded source PNG/BMP). `app_preview.png` and the
phone-side `app/` tree are **not** streamed. Implemented in `starmax_client/dialtranscode.py`
(`transcode_zip`); needs the `transcode` extra (Pillow + lz4). `lz4` is not canonical, so a
re-encoded container is watch-valid but not byte-identical to the app's — only the **decode**
direction is byte-exact.

## 3. Using `dial-push`

```
# preview the transfer plan (no radio):
starmax-client dial-push <native.blob> --dial-id 25001 --dry-run
# stream it and auto-activate (confirms via a dial-list read):
starmax-client dial-push <native.blob> --dial-id 25001
```

`dial-push` accepts **either** a native container **or** a distributed dial `.bin` (ZIP) — a
ZIP is transcoded on the fly (§2.1). Or transcode offline first with `dial-build <zip> <out>`.
The D-plane frames are sent **write-with-response** (reliable delivery — see the Flow-control
note in §1); the push aborts before sending if the negotiated MTU can't fit a full `D2` chunk. After streaming it
re-reads the dial list and reports whether `custom_id_<id>.bin` became the active face.

*Live-validated on hardware:* pushing the captured container installed and auto-activated the
face on a real GTX2 (dial-list `active=custom_id_25022.bin`, count 7→8).

## 4. Follow-ons
* **Windowed ack-paced flow control** — surface the watch's D2 byte-count acks (needs a small
  raw-notify hook in `transport.py`; the inbound path routes everything through the C1
  reassembler, which drops raw D-plane acks) to pace by acknowledged bytes. This is a **speed**
  optimization only — write-with-response already gives correct delivery (§1.2).
* **Switch among installed faces** — `dial-activate` reuses the `[SCHEMA/INFERRED]` `0x16`
  switch; the standalone switch opcode is still uncaptured (install itself auto-activates).
