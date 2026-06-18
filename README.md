# Setouchi Factory View — Notion → GitHub Pages 公開キット

Notion を管理画面（CMS）にして、「このデザインのまま」のサイトを GitHub Pages に公開するための一式です。
Notion で内容を更新 → GitHub のボタンを押す → 数分で公開サイトに反映されます。

---

## 仕組み

1. `build.py` が Notion API で 3つのデータベース（お知らせ / イベント紹介ページ / スケジュール）を読み込みます。
2. トップページのデザイン（`templates/index.html`）をそのまま使い、スケジュールとナビを差し替えます。
3. `public/` に下記を生成します。
   - `index.html` （トップ、最新スケジュール反映済み）
   - `news/` （お知らせ一覧 + 個別記事）
   - `factories/` （イベント紹介ページ一覧 + 個別ページ）
   - `assets/img/` （Notion に貼った画像をダウンロードして同梱）
4. GitHub Actions が `public/` を GitHub Pages に公開します。

公開されるのは `公開` チェックボックスが ON の行だけです（下書きは出ません）。

---

## セットアップ（初回のみ・約10分）

### 1. Notion インテグレーションを作る
1. https://www.notion.so/my-integrations を開く
2. 「New integration」で作成（タイプ: Internal）。名前例: `SFV Site Build`
3. 表示される `Internal Integration Secret`（`ntn_...`）を控える → これが `NOTION_TOKEN`

### 2. 3つの DB をインテグレーションに共有
各 DB を開き、右上 `…` メニュー → `コネクション`（Connections）から上記インテグレーションを追加します。
- お知らせ DB
- イベント紹介ページ DB
- スケジュール（プログラム一覧）DB

### 3. DB ID を控える
各 DB をブラウザで開き、URL の次の32桁が DB ID です。
`https://www.notion.so/<ワークスペース>/<この32桁がDB_ID>?v=...`

### 4. GitHub リポジトリを作る
1. GitHub で新規リポジトリを作成（Public / Private どちらでも可）
2. この `site-kit/` の中身をそのまま push
   ```
   cd site-kit
   git init
   git add .
   git commit -m "init SFV site"
   git branch -M main
   git remote add origin https://github.com/<user>/<repo>.git
   git push -u origin main
   ```

### 5. Secrets を登録
GitHub リポジトリ → Settings → Secrets and variables → Actions → `New repository secret`
で下記4つを登録：

| Name | 値 |
|------|-----|
| `NOTION_TOKEN` | 手順１の `ntn_...` |
| `NEWS_DB_ID` | お知らせ DB の ID |
| `PAGES_DB_ID` | イベント紹介ページ DB の ID |
| `SCHEDULE_DB_ID` | スケジュール DB の ID |

### 6. GitHub Pages を有効化
Settings → Pages → Build and deployment → Source を **GitHub Actions** に設定。

### 7. 初回ビルド
Actions タブ → 「Build & Deploy」ワークフロー → **Run workflow** ボタンを押す。
数分で完了し、Pages の URL（Settings → Pages に表示）で公開されます。

---

## 更新の流れ（毎回）
1. Notion の管理ページでお知らせ・紹介ページ・スケジュールを編集
2. GitHub の Actions タブ → Run workflow を押す
3. 数分で公開サイトに反映

定期自動化したい場合は `.github/workflows/deploy.yml` の `schedule:` コメントを外してください（例: 30分ごと）。

---

## ローカルで試す
```
cp config.example.env .env
# .env を編集して値を入れる
source .env
python3 build.py
# public/index.html をブラウザで開く
```
追加依存はありません（Python 標準ライブラリのみ）。

---

## Notion 側のプロパティ対応表

### お知らせ DB
- タイトル / 公開日（date）/ カテゴリ（select）/ 本文（概要）/ カバー画像（files）/ 公開（checkbox）
- 記事本文はそのページの中身（ブロック）がそのまま HTML 化されます。

### イベント紹介ページ DB
- ページタイトル / スラッグ（URL用）/ カバー写真 / ギャラリー写真 / 概要 / エリア / 並び順 / 公開
- 写真は Notion の file プロパティにアップロードしてください。ビルド時に取得し同梱します。

### スケジュール DB
- プログラム（title）/ 日程（date、複数日は期間で設定）/ カテゴリ / エリア / 会場 / 要予約 / 料金
- トップのカレンダーにそのまま反映されます。

---

## 注意点
- **反映は手動ビルド式**です（リアルタイムではありません）。Run workflow でいつでも即ビルドできます。
- Notion の画像 URL は有効期限付きのため、ビルド時にダウンロードしてリポジトリに同梱します（URL 切れの心配なし）。
- お知らせ DB にスラッグ列はないため、記事のファイル名はページ ID を使います。
- カスタムドメインを使う場合は GitHub Pages の Settings → Pages で設定できます。
