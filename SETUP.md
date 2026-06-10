# ぴっぴ クライアント セットアップ手順書

対象機材: Raspberry Pi 5  
作業PC: Windows (WSL) または Mac/Linux  
所要時間: 約15分

---

## 事前確認（必ず読む）

### ラズパイの IP アドレスを確認する

mDNS（`cosmos-robot.local` 等のホスト名）は環境によって使えない。  
**必ず IP アドレスで作業すること。**

ラズパイにモニター・キーボードを接続して：

```bash
hostname -I
```

表示された IP アドレス（例: `192.168.50.176`）を手元に控える。

### コマンドはすべて PC のターミナルで実行する

SSH 接続中のラズパイ側で実行するとエラーになるコマンドが多い。  
**プロンプトが `offic@CTS-074` であることを確認してから各コマンドを実行すること。**

---

## 手順

### 1. SSH 接続を確認する

```bash
ssh pi@<IPアドレス>
```

ログインできれば OK。確認後 `exit` で PC に戻る。

---

### 2. ファイルをラズパイに転送する

git clone はラズパイから GitHub に接続できないため使わない。rsync で転送する。

```bash
rsync -av /home/offic/RPi5/ pi@<IPアドレス>:/home/pi/cosmos-robot/
```

ファイル一覧が流れれば成功。

---

### 3. セットアップスクリプトを実行する

`sudo` を使うため `-t` オプションが必須。**`-t` を忘れるとパスワード入力に失敗してインストールが止まる。**

```bash
ssh -t pi@<IPアドレス> "cd /home/pi/cosmos-robot && bash install.sh"
```

完了すると以下が表示される：
```
=== 完了 ===
```

---

### 4. API キーを設定する

```bash
ssh pi@<IPアドレス> "echo 'PIPPI_API_KEY=<APIキー>' > /home/pi/cosmos-robot/.env"
```

`<APIキー>` は管理者から受け取った値に置き換える。

---

### 5. カメラ動作確認

```bash
ssh -t pi@<IPアドレス> "cd /home/pi/cosmos-robot && source .venv/bin/activate && python3 check_cameras.py"
```

以下のような出力が出れば正常：
```
使えるカメラ: インデックス [0, 2, 3, 4]
```

カメラが1台も表示されない場合はカメラの接続を確認する。

---

### 6. 起動コマンド

```bash
ssh -t pi@<IPアドレス> "cd /home/pi/cosmos-robot && source .venv/bin/activate && python3 pippi_client/access.py --detector mediapipe --camera 0"
```
