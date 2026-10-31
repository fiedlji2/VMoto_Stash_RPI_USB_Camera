# Protokol Carbit / EasyConn (displej motorky ↔ telefon) – ověřeno v praxi

Ověřeno na displeji Vmoto Stash (Carbit, `sdkVersion 0.9.23.3`) s Raspberry Pi 4 v roli „telefonu“.
Vše níže vychází ze zachyceného provozu a z dokumentace rozhraní nutné pro interoperabilitu.
Konkrétní identifikátory displeje jsou nahrazené `<HUID>`, `<HUName>`, `<MAC>`.

## 1. Wi-Fi Direct
- Displej je **autonomní GO** (skupina `DIRECT-xx`), P2P zařízení `RM_<HUID>`, MAC `<MAC>`
  (HUID na displeji = „E“ + MAC bez dvojteček). Poslech i provoz na **5745 MHz (kanál 149)**.
- Připojení: `p2p_prov_disc <MAC> pbc join` → `p2p_connect <MAC> pbc join freq=<listen_freq>` (WPS PBC, bez hesla).
  GO negotiation displej odmítá (už je GO).
- DHCP od displeje: telefon `192.168.2.20`, displej `192.168.2.1` (DHCP nabízí i výchozí bránu – ignorovat).
- **Pi 4 / 3 A+ vestavěná Wi-Fi (Broadcom):** firmware má pro EU kanály 149–165 zakázané →
  `iw reg set US` + `iw dev wlan0 set txpower fixed 1300` (13 dBm; v EU je 5725–5875 MHz povoleno do 25 mW).
- **NetworkManager** cizí P2P skupinu okamžitě zruší → wlan0 musí být pro NM `unmanaged`.

## 2. Objevení (mDNS)
Displej inzeruje `EasyConn._EasyConn._tcp.local` → `buildroot.local:10930`, TXT:
`flavor=51 huname=<HUName> port=10930 channel=21321 packagename=net.easyconn.easyride.wws huid=<HUID> ip=192.168.2.1`

Displej se navíc každé ~4 s připojí na **TCP 54321** telefonu a hned odpojí (kontrola, nic nepošle).

## 3. Rámec PXC (porty 10930, 10922)
16 B little-endian: `u32 cmd | u32 total_len | u32 cmd^total_len | u32 x` + payload (JSON).
Odpověď = `cmd + 1`. Pole `x` displej plní (význam neznámý), posílat 0 nevadí.

## 4. Sekvence
| # | Směr | Port | cmd | Obsah |
|---|---|---|---|---|
| 1 | telefon → displej | 10930 | `0x70000010` | `{"phoneType":"Android","packageName":"net.easyconn.easyride.wws"}` |
| 2 | displej → telefon | 10930 | `0x70000011` | `{"status":true}` |
| 3 | displej → telefon | 10922 (spoj. A) | `0x00010000` | typ kanálu C2P, odpověď `0x00010001` |
| 4 | displej → telefon | A | `0x00010010` | CLIENT_INFO (JSON ~1,1 kB: HUID, HUName, sdkVersion, …) |
| 5 | telefon → displej | A | `0x00010011` | odpověď telefonu (viz níže) |
| 6 | displej → telefon | A | `0x00010690` | `{"usbSpeed":0,"wifiSpeed":0}` |
| 7 | displej → telefon | 10922 (spoj. B) | `0x00020000` | typ kanálu P2C, odpověď `0x00020001` |
| 8 | displej → telefon | A | `0x70000000` | heartbeat, odpověď `0x70000001` (prázdná) |
| 9 | displej → telefon | A | `0x000103E0` | kontrola SN `{"client_set":"easy_conn","sn":"…"}` |
| 10 | telefon → displej | B | `0x000201C0` | výsledek kontroly SN (jako telefon bez internetu, viz níže) |
| 11 | telefon → displej | B | `0x00020020` | **APPSTATUS_FOREGROUND – bez něj displej video bere, ale nezobrazí** |
| 12 | telefon → displej | B | `0x70000000` | heartbeat telefonu ~1×/s, **nutný** – bez něj displej po ~7 s vše zavře |
| 13 | displej → telefon | 10921 | `0x0010` | REQ_RV_CONFIG_CAPTURE (kap. 5) → `0x0011` |
| 14 | displej ↔ telefon | 10921 | `0x0040`/`0x0041` | heartbeat médií (1×/s) |
| 15 | displej → telefon | 10921 | `0x0070` → `0x0071` | start dat |
| 16 | displej → telefon | 10920 | `0x0072` | „dej snímek“ → `u32 délka` + 1 snímek H.264 |

Těla:
- `0x00020020`: `{"displayRotation":1,"width":1024,"height":592,"enableAccessibility":false,"enableAOAHid":false}`
- `0x000201C0`: `{"isOk":true,"errCode":-100,"errMsg":"Network Not Available","id":"<sn>","client_set":"easy_conn"}`
  (stejné chování jako oficiální aplikace bez připojení k internetu)

### Odpověď na CLIENT_INFO (0x00010011)
Klíče: `pxcVersion "1.0.2"`, `phoneUUID`, `phoneBrand`, `phoneModel`, `phoneOsVersion`, `phoneOs "Android"`,
`package`, `versionCode`, `token 0`, `pubkey`, `encryptedHUID`, `bluetoothName`, `supportH264IFrame true`,
`supportFunction`, `appVersionFingerPrint`.
- `pubkey` = base64 DER SubjectPublicKeyInfo **vlastního** RSA-1024 páru telefonu (aplikace si ho generuje sama).
- `encryptedHUID` = HUID zašifrované soukromým klíčem telefonu (RSA PKCS#1 v1.5, blok typu 1), base64.
- Žádné klíče výrobce nejsou potřeba.

## 5. Mediální kanály 10921 (řízení) a 10920 (data)
Hlavička 8 B LE: `u16 cmd | u16 délka těla | u32 token (0)`.

**REQ_RV_CONFIG_CAPTURE (0x10)**, tělo 32 B + encryptedHUID:
`i16 šířka 1024 | i16 výška 592 | i32 wantFps 0 | i32 wantEncoder 2 (H.264) | i32 supportCodec 2 |
u16 minQ | u16 maxQ | i32 bitRate 8388608 | u8 capScreenMode | u8 touchMode | u8 orientation |
u8 displayId | u8 videoType | u8 supportExtendProtocol | 2 B rezerva | encryptedHUID (vrácené)`

**Odpovědi na 10921:** `0x11` = `i32 encoder 2 | i16 w | i16 h`; `0x31` = `i32 5002 | i32 1`;
`0x41` prázdné; `0x61` JSON `{"supportFunction":0}`; `0x71` prázdné; `0x111` `"OK\0"`; jinak `0x7F02`.

**Data 10920:** displej pošle `0x72`, telefon odpoví `u32 LE délka` + access unit H.264 Annex-B
(SPS/PPS opakovat). `u32 0` = snímek není. Tempo řídí displej (pull).

## 6. Video – co funguje nejlépe (naměřeno)
- **H.264 Baseline, jen úplné snímky (IDR každý snímek)** a na každou žádost `0x72` poslat **nejnovější** snímek:
  starší se zahodí bez rozbití obrazu, zpoždění se nehromadí. S rozdílovými snímky si displej bral méně snímků/s.
- Displej si řekne o ~23–26 snímků/s. SPS z HW enkodéru Pi už obsahuje `bitstream_restriction`
  (`max_num_reorder_frames=0`, `max_dec_frame_buffering=1`), dekodér nic nedrží v zásobě.
- **USB kamera (UVC MJPEG 1280×720@30):** SW `jpegdec` je o ~30 ms rychlejší než HW `v4l2jpegdec`
  (ten drží snímek); HW `v4l2convert` se v souběhu s HW dekodérem a enkodérem zasekává → SW zmenšení.
- Datový tok 12 Mbit/s (~50 kB/snímek) odstraní viditelné barevné přechody; Wi-Fi Direct (~72 Mbit/s) má rezervu.
- **Celkové zpoždění kamera → displej ~150 ms** (měřeno stopkami).
