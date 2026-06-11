# ぴっぴ クライアント セットアップ手順書

対象機材: Raspberry Pi 5  
作業PC: Windows (WSL) または Mac/Linux  
所要時間: 約15分

---

## 事前確認（必ず読む）

### コマンドはすべて PC のターミナルで実行する

SSH 接続中のラズパイ側で実行するとエラーになるコマンドが多い。  
**プロンプトが `offic@CTS-074` であることを確認してから各コマンドを実行すること。**

### 2つの IP アドレスを手元に控える

**ラズパイの IP アドレス**（ラズパイにモニター・キーボードを接続して）：
```bash
hostname -I
```

**PC（サーバー）の IP アドレス**（PC のターミナルで）：
```bash
ip route get 8.8.8.8 | grep src | awk '{print $7}'
```

---

## 初回セットアップ

### 1. ファイルをラズパイに転送する

```bash
PI_HOST=pi@<ラズパイのIP> bash /home/offic/cosmos-robot_/cosmos-robot/deploy/deploy.sh
```

> 初回はサービスがないため末尾にエラーが出るが無視してよい。

---

### 2. 環境を構築する

`-t` オプション必須：

```bash
ssh -t pi@<ラズパイのIP> "cd /home/pi/cosmos-robot && bash deploy/install.sh"
```

`=== セットアップ完了 ===` と表示されれば OK。

---

### 3. API キーとサーバー URL を設定する

```bash
ssh pi@<ラズパイのIP> "printf 'PIPPI_API_KEY=GvUzCBLHj0bgUqogEWuKv-NxO3LSYjxW9sKHHXWRGpU\nGYOMU_WS_URL=ws://<PCのIP>:8001/ws/robot\n' > /home/pi/cosmos-robot/.env"
```

---

### 4. 起動確認

```bash
ssh pi@<ラズパイのIP> "sudo systemctl start pippi-robot && sudo systemctl status pippi-robot"
```

`active (running)` と表示されれば完了。以後は電源を入れると自動起動する。

---

## コード更新（2回目以降）

```bash
PI_HOST=pi@<ラズパイのIP> bash /home/offic/cosmos-robot_/cosmos-robot/deploy/deploy.sh
```

これだけでファイル転送とサービス再起動が完了する。

---

## カメラ動作確認

```bash
ssh -t pi@<ラズパイのIP> "cd /home/pi/cosmos-robot && source .venv/bin/activate && python3 client/check_cameras.py"
```

`使えるカメラ: インデックス [0, 2, 3, 4]` と表示されれば正常。
