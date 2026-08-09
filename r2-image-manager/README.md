# R2 图片管理器

一个可直接部署到 Cloudflare Pages 的 React 图片管理后台。Pages Functions 使用 R2 binding 读写 Bucket，前端与 API 同域。

## 功能

- 管理令牌登录（HttpOnly、Secure、SameSite Cookie）
- 多图拖拽上传，流式写入 R2
- JPEG / PNG / WebP / GIF / AVIF 格式校验
- 单张图片 10 MiB 上限
- 图片分页列表、懒加载预览、原图查看和删除
- 响应式桌面/手机界面

## 1. 配置 R2 Bucket

项目已配置为使用当前账户中实际存在的 R2 Bucket：

```jsonc
"bucket_name": "images"
```

`binding` 必须保持为 `IMAGES`，因为 Pages Functions 通过 `env.IMAGES` 访问它。

## 2. 本地开发

```bash
cd r2-image-manager
npm install
cp .dev.vars.example .dev.vars
```

把 `.dev.vars` 中的 `ADMIN_TOKEN` 换成随机长字符串，然后运行：

```bash
npm run preview
```

打开 Wrangler 输出的本地地址。默认使用本地模拟的 R2 数据，不会改动线上 Bucket。

仅开发前端样式时可运行 `npm run dev`；这个模式不包含 Pages Functions，因此 API 会不可用。

## 3. 部署到 Cloudflare Pages

先登录并创建 Pages 项目（已存在同名项目时跳过创建）：

```bash
npx wrangler login
npx wrangler pages project create r2-image-manager
```

交互式设置生产环境管理令牌，令牌不会出现在命令历史或代码仓库中：

```bash
npx wrangler pages secret put ADMIN_TOKEN --project-name r2-image-manager
```

部署：

```bash
npm run deploy
```

`wrangler.jsonc` 是 R2 binding 的配置来源；`ADMIN_TOKEN` 是 Pages 加密 Secret，必须通过 `wrangler pages secret put` 或 Dashboard 单独设置。若 Pages 项目原本已在 Dashboard 配置过 binding，部署前请确认两边一致。

### 使用 Git 集成

在 Pages 项目中设置：

- Root directory：`r2-image-manager`
- Build command：`npm run build`
- Build output directory：`dist`

然后在 **Settings → Variables and Secrets** 添加加密变量 `ADMIN_TOKEN`。R2 binding 可由仓库中的 `wrangler.jsonc` 管理；若改用 Dashboard，变量名仍须为 `IMAGES`。

## 常用命令

```bash
npm run typecheck  # TypeScript 检查
npm run build      # 生产构建
npm run preview    # Pages + Functions + 本地 R2
npm run cf-typegen # 配置变化后重新生成 Cloudflare 类型
```

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/api/session` | 验证管理令牌并建立会话 |
| `DELETE` | `/api/session` | 退出会话 |
| `GET` | `/api/images` | 分页列出图片 |
| `POST` | `/api/images?filename=...` | 上传图片二进制流 |
| `GET` | `/api/images/:key` | 读取图片 |
| `DELETE` | `/api/images/:key` | 删除图片 |

生产环境建议同时使用 Cloudflare Access、WAF 或 Rate Limiting 为管理页面增加第二层保护。
