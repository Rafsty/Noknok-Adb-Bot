# topnod

Toolkit otomasi untuk mengulang flow onboarding Android lewat perintah `adb`, sambil membuat akun mail.tm sekali pakai dan membaca OTP secara otomatis. Semua koordinat tap/scroll/prompt ditulis di file teks biasa seperti `kordinat2.txt`, jadi alur bisa disesuaikan tanpa mengubah kode Python.

## Fitur
- Parse skrip koordinat untuk menjalankan tap, jeda, scroll, dan input teks via `adb shell input`.
- Membuat inbox mail.tm baru di setiap run, menarik OTP otomatis, dan fallback ke input manual jika gagal.
- Menyimpan akun sukses (email/password) ke `Mail.txt` (dipisah pipa) sekaligus `created_accounts.jsonl` (JSONL).
- Mendukung batch akun (`--count`), dry-run, pengaturan delay, serta loop otomatis (`back to no 1`).
- Modul opsional `mailtm.py` memakai `requests` agar polling OTP lebih cepat; jika tidak ada, klien bawaan memakai `urllib`.

## Struktur repo
- `main.py` – CLI utama untuk parse flow koordinat, bikin inbox mail.tm, dan replay langkah di device.
- `mailtm.py` – wrapper ringan REST mail.tm berbasis `requests`, sekaligus tulis log ke `Mail.txt`.
- `kordinat2.txt` – flow koordinat default yang dipakai `main.py`, lengkap dengan tap, jeda, OTP, dan penanda loop.
- `koordinat.txt` – versi flow alternatif/legacy untuk referensi.
- `Mail.txt` – log `email|password` yang diappend oleh `mailtm.py`.
- `created_accounts.jsonl` – log JSONL yang dibuat runtime oleh `main.py`.

## Kebutuhan sistem
- Windows 10/11 dengan Python 3.10+ (teruji) dan akses `adb`.
- Device/emulator Android dengan USB debugging aktif (atau set env `ADB_SERIAL`).
- Internet ke `https://api.mail.tm`.
- Opsional: paket `requests` (dan `colorama` bila ingin log berwarna) untuk `mailtm.py`.

Instal dependensi Python sekali saja:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip requests colorama
```

Pastikan `adb.exe` ada di `PATH` atau berikan argumen `--adb-path`.

## Langkah cepat
1. Sambungkan satu device/emulator Android lalu cek `adb devices`.
2. Edit `kordinat2.txt` bila perlu menyesuaikan koordinat dengan resolusi layar.
3. Jalankan:
   ```powershell
   python main.py --flow kordinat2.txt --count 3 --tap-delay 0.15 --text-delay 0.25
   ```
4. Saat diminta, masukkan kode referral (sekalian dipakai sebagai password). Script akan membuat akun mail.tm, mengambil OTP, lalu mengeksekusi tap.
5. Setelah setiap akun, ikuti instruksi di terminal untuk reset UI jika flow tidak punya `back to no 1`.

Gunakan `--dry-run` untuk hanya menampilkan langkah tanpa menekan device:

```powershell
python main.py --dry-run --flow kordinat2.txt
```

## Argumen CLI penting
- `--count N` – jumlah akun yang dibuat (default 5).
- `--serial SERIAL` / env `ADB_SERIAL` – pilih device jika lebih dari satu.
- `--flow FILE` – file skrip koordinat (default `kordinat2.txt`).
- `--otp-regex REGEX` – pola capture OTP (default enam digit).
- `--otp-timeout` / `--otp-poll` – atur batas waktu dan interval polling OTP.
- `--tap-delay` / `--text-delay` – jeda setelah tap/input teks; bisa dikombinasikan dengan `--implicit-delay`.
- `--scroll-count`, `--scroll-duration-ms`, `--scroll-pause` – tuning gesture swipe untuk step `scroll`.
- `--enter-timeout` – ubah prompt “tekan Enter” jadi auto lanjut setelah N detik.
- `--no-enter-next` – hilangkan jeda antar akun (hanya aman jika flow reset sendiri).
- `--prefer-mailtm-module` – paksa pakai `mailtm.py` (butuh `requests`).

Lihat `python main.py --help` untuk daftar lengkap.

## Format skrip koordinat
Setiap baris non-kosong diparse menjadi `Step`. Beberapa pola yang dikenali:

| Contoh baris | Hasil |
| --- | --- |
| `4. x;177 y;452 tap dlu baru isi email` | Tap di (177,452) lalu otomatis isi email yang digenerate. |
| `13. x;507 y;1630 tunggu 6 detik sblm tap` | Tunggu 6 detik, tap, kemudian apply implicit delay. |
| `sekrol sampe mentol` / `scroll` | Memicu swipe sesuai parameter `--scroll-*`. |
| `back to no 1` | Menandai loop, jadi akun berikutnya langsung mulai. |
| `isi otp`, `kode referral`, `isi password`, `retype password` | Menambah step `text_*` supaya input diarahkan ke field yang benar. |

Tips:
- Pastikan koordinat cocok dengan resolusi device; gunakan `adb shell getevent` atau `adb shell uiautomator dump` saat mengambil titik baru.
- Untuk area CAPTCHA manual, tinggalkan catatan seperti langkah 5 di `kordinat2.txt`; runner akan jeda OTP sampai lanjut lagi.
- Jika butuh penyesuaian manual antar akun, hapus `back to no 1`; script akan meminta Enter sebelum lanjut.

## OTP & pencatatan akun
- Jika `mailtm.py` tersedia (atau diaktifkan via `--prefer-mailtm-module`), modul tersebut membuat mailbox random, login, dan menjalankan thread `start_otp_prefetch` berbasis `requests`.
- Tanpa `mailtm.py`, `main.py` memakai kelas `MailTm` bawaan yang langsung hit API `https://api.mail.tm` via `urllib`.
- Jika OTP gagal terbaca, pengguna akan diminta memasukkan kode secara manual.
- Akun sukses ditulis ke `created_accounts.jsonl`, misalnya:
  ```json
  {"created_at":"2026-03-17T16:40:52+0700","serial":"emulator-5554","email":"uabc123@domain","password":"refcode"}
  ```

## Troubleshooting
- **Device lebih dari satu:** set `ADB_SERIAL=emulator-5554` atau gunakan `--serial`.
- **OTP lama muncul:** cek status mail.tm, naikkan `--otp-timeout`, atau lihat `Mail.txt` apakah akun benar-benar dibuat.
- **Tap meleset:** sesuaikan koordinat dengan densitas layar, atau tambahkan instruksi jeda (`tunggu 3 detik sblm tap`) agar layar sempat stabil.
- **Butuh stop cepat:** tekan `Ctrl+C` dua kali; sinyal pertama meminta stop halus setelah aksi berjalan.

## Lisensi
Belum ada file lisensi. Tambahkan sebelum repo dipublikasikan secara publik.
