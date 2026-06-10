# ぴっぴ クライアント セットアップ手順書

対象機材: Raspberry Pi 5  
作業PC: Windows (WSL) または Mac/Linux  
所要時間: 約15分

---

## 前提条件

- ラズパイに Raspberry Pi OS がインストール済み
- ラズパイとPCが同じWi-Fiに接続済み
- ラズパイの SSH が有効（Raspberry Pi Imager で設定済み）
- ラズパイの IPアドレスが判明している（不明な場合は後述）

---

## 1. ラズパイの IP アドレスを確認する

ラズパイにモニター・キーボードを接続して：

```bash
hostname -I
```

表示された IP アドレス（例: `192.168.50.176`）を控える。

---

## 2. PC から SSH 接続を確認する

**PC のターミナル**（WSL または Terminal）で：

```bash
ssh pi@<IPアドレス>
```

パスワードを入力してログインできれば OK。確認後 `exit` でPCに戻る。

---

## 3. ファイルをラズパイに転送する

**PC のターミナル**で実行する（ラズパイ側ではない）：

```bash
rsync -av /home/offic/RPi5/ pi@<IPアドレス>:/home/pi/cosmos-robot/
```

パスワードを入力する。ファイル一覧が流れれば成功。

---

## 4. セットアップスクリプトを実行する

**PC のターミナル**で実行する。`-t` オプション必須（sudo がパスワードを求めるため）：

```bash
ssh -t pi@<IPアドレス> "cd /home/pi/cosmos-robot && bash install.sh"
```

パスワードを2回聞かれる場合がある（SSH接続時・sudo実行時）。

完了すると以下が表示される：
```
=== 完了 ===
次: nano .env  →  source .venv/bin/activate  →  python3 check_cameras.py
```

---

## 5. API キーを設定する

**PC のターミナル**で実行する：

```bash
ssh pi@<IPアドレス> "echo 'PIPPI_API_KEY=<APIキー>' > /home/pi/cosmos-robot/.env"
```

`<APIキー>` は管理者から受け取った値に置き換える。

---

## 6. カメラ動作確認

**PC のターミナル**で実行する：

```bash
ssh -t pi@<IPアドレス> "cd /home/pi/cosmos-robot && source .venv/bin/activate && python3 check_cameras.py"
```

以下のような出力が出れば正常：
```
使えるカメラ: インデックス [0, 2, 3, 4]
```

カメラが1台も表示されない場合はカメラの接続を確認する。

---

## 7. 起動コマンド（確認後）

```bash
ssh -t pi@<IPアドレス> "cd /home/pi/cosmos-robot && source .venv/bin/activate && python3 pippi_client/access.py --detector mediapipe --camera 0"
```

---

## よくあるエラーと対処

| エラー | 原因 | 対処 |
|--------|------|------|
| `No such file or directory` | ラズパイ側で PC 向けコマンドを実行している | `exit` で PC に戻ってから実行 |
| `remote helper 'https' aborted` | ラズパイから GitHub に git clone できない | rsync で転送する（手順3） |
| `externally-managed-environment` | venv を有効化せずに pip を実行 | `source .venv/bin/activate` してから実行 |
| `sudo: パスワードが必要` | SSH 経由で sudo を実行しているが `-t` がない | `ssh -t` を使う（手順4） |
| `No module named 'cv2'` | venv が `--system-site-packages` なしで作られている | `.venv` を削除して `bash install.sh` を再実行 |
| `Could not resolve hostname cosmos-robot.local` | mDNS が使えない環境 | IP アドレスを直接使う（手順1） |
