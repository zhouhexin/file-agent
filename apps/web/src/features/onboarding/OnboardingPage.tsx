// 独立 /getting-started 新手引导页：展示功能卡片和示例问题，
// 写入 localStorage 后跳转回 Chat。

import { useEffect, useMemo, useState } from 'react';
import { ArrowRight, MessageSquare, RefreshCcw } from 'lucide-react';
import { ApiError, getAgentCapabilities } from '../../api/client';
import { formatError } from '../../api/errors';
import type { AgentCapability } from '../../types';
import { FALLBACK_ONBOARDING_CARDS, type OnboardingCard } from './onboardingCards';
import './onboarding.css';

type OnboardingPageProps = {
  token: string;
  onStart: () => void;
  onBackToChat: () => void;
  onTryExample: (example: string) => void;
};

function iconForCapability(id: string): string {
  // 后端能力清单不带 icon 字段，按 id 在本地 fallback 中查找匹配图标。
  const fallback = FALLBACK_ONBOARDING_CARDS.find((item) => item.id === id);
  return fallback?.icon ?? '✨';
}

function toCards(capabilities: AgentCapability[]): OnboardingCard[] {
  return capabilities.map((capability) => ({
    ...capability,
    icon: iconForCapability(capability.id),
  }));
}

export function OnboardingPage({
  token,
  onStart,
  onBackToChat,
  onTryExample,
}: OnboardingPageProps) {
  const [cards, setCards] = useState<OnboardingCard[]>(FALLBACK_ONBOARDING_CARDS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    // 拉取后端能力清单；失败或返回空时使用本地 fallback。
    let cancelled = false;
    setLoading(true);
    setError('');

    getAgentCapabilities(token)
      .then((catalog) => {
        if (cancelled) {
          return;
        }
        const nextCards = toCards(catalog.capabilities);
        setCards(nextCards.length > 0 ? nextCards : FALLBACK_ONBOARDING_CARDS);
      })
      .catch((err) => {
        if (cancelled) {
          return;
        }
        setCards(FALLBACK_ONBOARDING_CARDS);
        setError(err instanceof ApiError ? formatError(err) : '能力清单加载失败，已使用本地默认介绍。');
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [token]);

  const primaryCards = useMemo(() => cards.slice(0, 8), [cards]);

  return (
    <main className="onboarding-page">
      <section className="onboarding-hero">
        <div>
          <p className="eyebrow">File Agent 新手引导</p>
          <h1>用自然语言处理文件、表格和受管目录</h1>
          <p className="hero-description">
            这里展示常用能力和示例问题。你可以直接点击示例进入聊天，也可以先浏览后开始使用。
          </p>
        </div>
        <div className="hero-actions">
          <button className="ghost-button" type="button" onClick={onBackToChat}>
            <MessageSquare size={18} /> 返回聊天
          </button>
          <button className="primary-button" type="button" onClick={onStart}>
            开始使用 <ArrowRight size={18} />
          </button>
        </div>
      </section>

      {loading ? <p className="onboarding-status">正在加载功能清单...</p> : null}
      {error ? <p className="onboarding-status warning">{error}</p> : null}

      <section className="onboarding-grid" aria-label="功能示例卡片">
        {primaryCards.map((card) => (
          <article className="onboarding-card" key={card.id}>
            <div className="card-icon" aria-hidden="true">{card.icon}</div>
            <h2>{card.name}</h2>
            <p>{card.description}</p>
            <div className="example-list">
              {card.examples.slice(0, 2).map((example) => (
                <button
                  className="example-chip"
                  key={example}
                  type="button"
                  onClick={() => onTryExample(example)}
                >
                  {example}
                </button>
              ))}
            </div>
          </article>
        ))}
      </section>

      <section className="onboarding-footer-card">
        <div>
          <h2>使用建议</h2>
          <p>上传文件后，直接说明你的目标，例如“总结”“分类”“统计金额”或“列出目录中的 PDF”。</p>
        </div>
        <button className="ghost-button" type="button" onClick={onStart}>
          <RefreshCcw size={18} /> 我知道了，进入聊天
        </button>
      </section>
    </main>
  );
}
