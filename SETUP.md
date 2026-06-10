# ぴっぴ クライアント セットアップ手順書

対象機材: Raspberry Pi 5  
作業PC: Windows (WSL) または Mac/Linux  
所要時間: 約15分

---

## 事前確認（必ず読む）

### コマンドはすべて PC のターミナルで実行する

SSH 接続中のラズパイ側で実行するとエラーになるコマンドが多い。  
**プロンプトが `offic@CTS-074` であることを確認してから各コマンドを実行すること。**

### ラズパイの IP アドレスを使う

ホスト名（`cosmos-robot.local` 等）は環境によって使えない。  
**必ず IP アドレスで作業すること。**

ラズパイにモニター・キーボードを接続して確認：

```bash
hostname -I
```

---

## 初回セットアップ

### 1. ファイルをラズパイに転送する

**PC のターミナル**で実行：

```bash
PI_HOST=pi@<IPアドレス> bash deploy/deploy.sh
```

ファイル転送とサービス再起動が自動で行われる。

> 初回は systemd サービスがないためサービス再起動でエラーになるが無視してよい。

---

### 2. セットアップスクリプトを実行する

`-t` オプション必須（sudo がパスワードを求めるため）：

```bash
ssh -t pi@<IPアドレス> "cd /home/pi/cosmos-robot && bash deploy/install.sh"
```

完了すると以下が表示される：
```
=== セットアップ完了 ===
```

---

### 3. API キーとサーバー URL を設定する

```bash
ssh pi@<IPアドレス> "cat > /home/pi/cosmos-robot/.env" << 'EOF'
PIPPI_API_KEY=<APIキー>
GYOMU_WS_URL=ws://<サーバーIPアドレス>:8001/ws/robot
EOF
```

---

### 4. 起動確認

```bash
ssh pi@<IPアドレス> "sudo systemctl start pippi-robot && sudo systemctl status pippi-robot"
```

`active (running)` と表示されれば完了。以後は電源を入れると自動起動する。

---

## コード更新（2回目以降）

**PC のターミナル**で：

```bash
PI_HOST=pi@<IPアドレス> bash deploy/deploy.sh
```

これだけでファイル転送とサービス再起動が完了する。

---

## カメラ動作確認

```bash
ssh -t pi@<IPアドレス> "cd /home/pi/cosmos-robot && source .venv/bin/activate && python3 check_cameras.py"
```

以下のような出力が出れば正常：
```
使えるカメラ: インデックス [0, 2, 3, 4]
```
