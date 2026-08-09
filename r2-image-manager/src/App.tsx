import { ChangeEvent, DragEvent, FormEvent, useCallback, useEffect, useRef, useState } from "react";

type ImageItem = {
  key: string;
  name: string;
  size: number;
  uploaded: string;
  contentType: string;
  url: string;
};

type ListResponse = { images: ImageItem[]; cursor: string | null };
type ApiError = { error?: string };

const ACCEPTED_TYPES = new Set(["image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"]);
const MAX_FILE_SIZE = 10 * 1024 * 1024;

async function api<T>(input: RequestInfo | URL, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => ({}));
    const message = typeof body === "object" && body !== null && "error" in body
      ? (body as ApiError).error
      : undefined;
    throw new Error(message || `请求失败（${response.status}）`);
  }
  return response.json() as Promise<T>;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [token, setToken] = useState("");
  const [images, setImages] = useState<ImageItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const loadImages = useCallback(async (nextCursor?: string) => {
    setLoading(true);
    try {
      const query = nextCursor ? `?cursor=${encodeURIComponent(nextCursor)}` : "";
      const data = await api<ListResponse>(`/api/images${query}`);
      setImages((current) => nextCursor ? [...current, ...data.images] : data.images);
      setCursor(data.cursor);
      setAuthenticated(true);
    } catch (error) {
      if (error instanceof Error && error.message === "请先登录") setAuthenticated(false);
      else setNotice(error instanceof Error ? error.message : "加载图片失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadImages();
  }, [loadImages]);

  async function login(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setNotice(null);
    try {
      await api<{ ok: true }>("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      setToken("");
      await loadImages();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "登录失败");
    } finally {
      setLoading(false);
    }
  }

  async function logout() {
    await fetch("/api/session", { method: "DELETE" });
    setImages([]);
    setAuthenticated(false);
  }

  async function uploadFiles(files: File[]) {
    const valid: File[] = [];
    for (const file of files) {
      if (!ACCEPTED_TYPES.has(file.type)) {
        setNotice(`${file.name} 不是支持的图片格式`);
      } else if (file.size > MAX_FILE_SIZE) {
        setNotice(`${file.name} 超过 10 MiB`);
      } else {
        valid.push(file);
      }
    }
    if (!valid.length) return;

    setNotice(null);
    setUploading(valid.map((file) => file.name));
    const uploaded: ImageItem[] = [];
    for (const file of valid) {
      try {
        const result = await api<{ image: ImageItem }>(
          `/api/images?filename=${encodeURIComponent(file.name)}`,
          {
            method: "POST",
            headers: {
              "Content-Type": file.type,
              "X-File-Size": String(file.size),
            },
            body: file,
          },
        );
        uploaded.push(result.image);
      } catch (error) {
        setNotice(`${file.name}: ${error instanceof Error ? error.message : "上传失败"}`);
      } finally {
        setUploading((current) => current.filter((name) => name !== file.name));
      }
    }
    setImages((current) => [...uploaded.reverse(), ...current]);
  }

  function chooseFiles(event: ChangeEvent<HTMLInputElement>) {
    void uploadFiles(Array.from(event.target.files || []));
    event.target.value = "";
  }

  function dropFiles(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    void uploadFiles(Array.from(event.dataTransfer.files));
  }

  async function deleteImage(image: ImageItem) {
    if (!window.confirm(`确定删除“${image.name}”吗？此操作无法撤销。`)) return;
    try {
      await api<{ ok: true }>(image.url, { method: "DELETE" });
      setImages((current) => current.filter((item) => item.key !== image.key));
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "删除失败");
    }
  }

  if (authenticated !== true) {
    return (
      <main className="login-shell">
        <section className="login-card">
          <div className="brand-mark">R2</div>
          <p className="eyebrow">Cloudflare image library</p>
          <h1>你的图片，安静地待在边缘。</h1>
          <p className="subtle">输入 Pages 项目中配置的管理令牌，进入图片库。</p>
          <form onSubmit={login}>
            <label htmlFor="token">管理令牌</label>
            <input
              id="token"
              type="password"
              autoComplete="current-password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="ADMIN_TOKEN"
              required
            />
            <button className="primary-button" disabled={loading || !token}>
              {loading ? "正在验证…" : "进入图片库"}
            </button>
          </form>
          {notice && <p className="notice" role="alert">{notice}</p>}
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">R2 image library</p>
          <h1>图片库</h1>
        </div>
        <div className="header-actions">
          <span className="count">{images.length} 张图片</span>
          <button className="quiet-button" onClick={logout}>退出</button>
        </div>
      </header>

      <section
        className={`drop-zone ${dragging ? "is-dragging" : ""}`}
        onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false);
        }}
        onDrop={dropFiles}
      >
        <div className="upload-icon">↑</div>
        <div>
          <h2>把图片拖到这里</h2>
          <p>JPEG、PNG、WebP、GIF 或 AVIF，单张不超过 10 MiB</p>
        </div>
        <button className="primary-button compact" onClick={() => fileInput.current?.click()}>
          选择图片
        </button>
        <input
          ref={fileInput}
          className="visually-hidden"
          type="file"
          accept="image/jpeg,image/png,image/webp,image/gif,image/avif"
          multiple
          onChange={chooseFiles}
        />
      </section>

      {uploading.length > 0 && (
        <section className="upload-status" aria-live="polite">
          <span className="spinner" />
          正在上传 {uploading.length} 张图片：{uploading[0]}
        </section>
      )}

      {notice && (
        <div className="notice banner" role="alert">
          <span>{notice}</span>
          <button onClick={() => setNotice(null)} aria-label="关闭">×</button>
        </div>
      )}

      <section className="library" aria-busy={loading}>
        <div className="section-heading">
          <h2>全部图片</h2>
          <button className="quiet-button" onClick={() => void loadImages()} disabled={loading}>
            {loading ? "刷新中…" : "刷新"}
          </button>
        </div>

        {!loading && images.length === 0 ? (
          <div className="empty-state">
            <span>◇</span>
            <h3>这里还没有图片</h3>
            <p>上传第一张图片，它会安全地存入你的 R2 Bucket。</p>
          </div>
        ) : (
          <div className="image-grid">
            {images.map((image) => (
              <article className="image-card" key={image.key}>
                <a className="image-frame" href={image.url} target="_blank" rel="noreferrer">
                  <img src={image.url} alt={image.name} loading="lazy" />
                </a>
                <div className="image-info">
                  <div>
                    <h3 title={image.name}>{image.name}</h3>
                    <p>{formatBytes(image.size)} · {formatDate(image.uploaded)}</p>
                  </div>
                  <button className="delete-button" onClick={() => void deleteImage(image)} aria-label={`删除 ${image.name}`}>
                    删除
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}

        {cursor && (
          <button className="load-more" onClick={() => void loadImages(cursor)} disabled={loading}>
            {loading ? "加载中…" : "加载更多"}
          </button>
        )}
      </section>
    </main>
  );
}

export default App;
