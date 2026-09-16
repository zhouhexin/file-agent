// 独立提示词指南页：静态展示已支持能力，并允许用户复制经过约束的示例提示词。

import { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Copy, Search, Sparkles } from 'lucide-react';

import { PROMPT_GUIDE_SECTIONS } from './promptGuideContent';
import './prompt-guide.css';

async function copyText(text: string): Promise<void> {
  // 非 HTTPS 或旧浏览器可能没有 Clipboard API，保留同步复制兜底。
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('COPY_FAILED');
}

export function PromptGuidePage() {
  const [query, setQuery] = useState('');
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [copyError, setCopyError] = useState('');
  const copiedTimerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (copiedTimerRef.current !== null) window.clearTimeout(copiedTimerRef.current);
  }, []);

  const filteredSections = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase('zh-CN');
    if (!keyword) return PROMPT_GUIDE_SECTIONS;
    return PROMPT_GUIDE_SECTIONS.flatMap((section) => {
      const sectionMatched = `${section.title} ${section.description}`
        .toLocaleLowerCase('zh-CN')
        .includes(keyword);
      const items = sectionMatched
        ? section.items
        : section.items.filter((item) => (
            `${item.title} ${item.prompt} ${item.channel} ${item.note ?? ''}`
              .toLocaleLowerCase('zh-CN')
              .includes(keyword)
          ));
      return items.length > 0 ? [{ ...section, items }] : [];
    });
  }, [query]);

  async function handleCopy(id: string, prompt: string) {
    try {
      setCopyError('');
      await copyText(prompt);
      setCopiedId(id);
      if (copiedTimerRef.current !== null) window.clearTimeout(copiedTimerRef.current);
      copiedTimerRef.current = window.setTimeout(() => setCopiedId(null), 1800);
    } catch {
      setCopyError('复制失败，请选中提示词后手动复制。');
    }
  }

  function scrollToSection(id: string) {
    document.getElementById(`prompt-section-${id}`)?.scrollIntoView({ behavior: 'smooth' });
  }

  return (
    <main className="prompt-guide-page">
      <header className="prompt-guide-hero">
        <div className="prompt-guide-heading">
          <div className="prompt-guide-title-row">
            <span className="prompt-guide-logo" aria-hidden="true"><Sparkles size={24} /></span>
            <div>
              <p className="prompt-guide-eyebrow">FILE AGENT 使用指南</p>
              <h1>常用提示词</h1>
            </div>
          </div>
          <p className="prompt-guide-intro">
            选择一个场景，复制示例后替换文件名、分类路径或授权目录。提示词越明确，文件定位和执行结果越可靠。
          </p>
        </div>
        <label className="prompt-guide-search">
          <Search size={18} aria-hidden="true" />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索：分类、附件、重命名……"
            aria-label="搜索提示词"
          />
        </label>
      </header>

      <section className="prompt-guide-rules" aria-label="提示词使用原则">
        <article>
          <strong>已入库文件</strong>
          <span>尽量提供包含扩展名的完整文件名。</span>
        </article>
        <article>
          <strong>分类与移动</strong>
          <span>同时写清完整文件名和完整目标路径。</span>
        </article>
        <article>
          <strong>当前附件</strong>
          <span>明确说“本轮附件”，并确保附件桥接套件已启用。</span>
        </article>
        <article>
          <strong>内容结论</strong>
          <span>要求返回页码、Sheet、单元格或原文依据。</span>
        </article>
      </section>

      <nav className="prompt-guide-nav" aria-label="提示词场景">
        {PROMPT_GUIDE_SECTIONS.map((section) => (
          <button key={section.id} type="button" onClick={() => scrollToSection(section.id)}>
            {section.title}
          </button>
        ))}
      </nav>

      {copyError ? <p className="prompt-guide-copy-error" role="alert">{copyError}</p> : null}

      <div className="prompt-guide-sections">
        {filteredSections.map((section) => (
          <section
            className="prompt-guide-section"
            id={`prompt-section-${section.id}`}
            key={section.id}
          >
            <header>
              <h2>{section.title}</h2>
              <p>{section.description}</p>
            </header>
            <div className="prompt-guide-grid">
              {section.items.map((item) => (
                <article className="prompt-guide-card" key={item.id}>
                  <div className="prompt-guide-card-heading">
                    <h3>{item.title}</h3>
                    <span className="prompt-guide-channel">{item.channel}</span>
                  </div>
                  <div className="prompt-guide-prompt-row">
                    <blockquote>{item.prompt}</blockquote>
                    <button
                      className={copiedId === item.id ? 'prompt-copy-button copied' : 'prompt-copy-button'}
                      type="button"
                      onClick={() => void handleCopy(item.id, item.prompt)}
                    >
                      {copiedId === item.id ? <Check size={16} /> : <Copy size={16} />}
                      {copiedId === item.id ? '已复制' : '复制提示词'}
                    </button>
                  </div>
                  {item.note ? <p className="prompt-guide-note">{item.note}</p> : null}
                </article>
              ))}
            </div>
          </section>
        ))}
      </div>

      {filteredSections.length === 0 ? (
        <section className="prompt-guide-empty">
          <h2>没有找到相关提示词</h2>
          <p>可以尝试搜索“分类”“附件”“查找”或“重命名”。</p>
          <button type="button" onClick={() => setQuery('')}>清除搜索</button>
        </section>
      ) : null}
    </main>
  );
}
