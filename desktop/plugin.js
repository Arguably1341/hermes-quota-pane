import { Button, DisclosureCaret, GlyphSpinner, host, PANES_AREA, ScrollArea, StatusDot, useQuery } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let api = null
let storage = null
const COLLAPSED_KEY = 'collapsed-providers'

function pct(value) {
  return typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value)}%` : '—'
}

function resetLabel(value) {
  if (!value) return ''
  const delta = new Date(value).getTime() - Date.now()
  if (!Number.isFinite(delta)) return ''
  if (delta <= 0) return 'reset pendente'
  const minutes = Math.ceil(delta / 60000)
  if (minutes < 60) return `reset em ${minutes}min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (hours < 48) return `reset em ${hours}h${rest ? ` ${rest}min` : ''}`
  return `reset em ${Math.floor(hours / 24)}d ${hours % 24}h`
}

function meterClass(remaining) {
  if (remaining <= 10) return 'bg-destructive'
  if (remaining <= 25) return 'bg-amber-500'
  return 'bg-primary'
}

function money(value) {
  return `$${value.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function collapsedSet() {
  const stored = storage?.get(COLLAPSED_KEY, [])
  return new Set(Array.isArray(stored) ? stored.filter(id => typeof id === 'string') : [])
}

function cardSummary(windows, balance) {
  if (balance !== null) return money(balance)
  const percents = windows
    .map(window => window.remaining_percent)
    .filter(value => typeof value === 'number' && Number.isFinite(value))
  if (!percents.length) return ''
  return percents.length === 1 ? pct(percents[0]) : percents.map(value => pct(value)).join(' · ')
}

function balanceFromDetails(provider) {
  const pattern = provider.provider === 'deepseek' || provider.provider === 'parallel'
    ? /^Saldo USD:\s*\$?([0-9]+(?:[.,][0-9]+)?)/i
    : provider.provider === 'openrouter'
      ? /^Credits balance:\s*\$?([0-9]+(?:[.,][0-9]+)?)/i
      : null
  if (!pattern) return null
  for (const detail of provider.details) {
    const match = detail.match(pattern)
    if (match) return Number(match[1].replace(',', '.'))
  }
  return null
}

// Escala da barra do saldo: DeepSeek/OpenRouter usam a referência histórica de
// US$ 10 = 100%. O Parallel é saldo pré-pago sem alvo definido, então não há
// percentual honesto a mostrar — devolve null e o card mostra só o número.
function balanceCeiling(provider) {
  return provider.provider === 'parallel' ? null : 10
}

function translatedDetail(provider, detail) {
  if (provider.provider !== 'openai-codex') return detail
  const match = detail.match(/^You have (\d+) resets? banked\s*-\s*use \/usage reset to activate$/i)
  if (!match) return detail
  const count = Number(match[1])
  return count === 1
    ? 'Você tem 1 reset acumulado — use /usage reset para ativá-lo'
    : `Você tem ${count} resets acumulados — use /usage reset para ativá-los`
}

function displayWindow(provider, window) {
  if (provider.provider !== 'openai-codex') return window
  if (window.label === 'Session') return { ...window, label: '5h' }
  if (window.label === 'Weekly') return { ...window, label: '7d' }
  return window
}

function WindowRow({ window }) {
  const remaining = typeof window.remaining_percent === 'number' ? window.remaining_percent : null
  return jsxs('div', {
    className: 'grid gap-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-baseline gap-2 text-xs',
        children: [
          jsx('span', { className: 'font-medium text-foreground', children: window.label }),
          jsx('span', { className: 'ml-auto tabular-nums text-(--ui-text-secondary)', children: `${pct(remaining)} restante` }),
          window.reset_at ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: resetLabel(window.reset_at) }) : null
        ]
      }),
      jsx('div', {
        className: 'h-1.5 overflow-hidden rounded-full bg-(--ui-control-bg)',
        children: jsx('div', {
          className: `h-full rounded-full transition-[width] ${meterClass(remaining ?? 100)}`,
          style: { width: `${Math.max(0, Math.min(100, remaining ?? 0))}%` }
        })
      }),
      window.detail ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: window.detail }) : null
    ]
  })
}

function BalanceRow({ balance, ceiling }) {
  const hasCeiling = typeof ceiling === 'number' && ceiling > 0
  const remaining = hasCeiling ? Math.max(0, Math.min(100, (balance / ceiling) * 100)) : null
  return jsxs('div', {
    className: 'grid gap-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-baseline gap-2 text-xs',
        children: [
          jsx('span', { className: 'font-medium text-foreground', children: `Saldo: ${money(balance)}` }),
          remaining === null ? null : jsx('span', { className: 'ml-auto tabular-nums text-(--ui-text-secondary)', children: `${pct(remaining)} disponível` })
        ]
      }),
      remaining === null ? null : jsx('div', {
        className: 'h-1.5 overflow-hidden rounded-full bg-(--ui-control-bg)',
        children: jsx('div', {
          className: `h-full rounded-full transition-[width] ${meterClass(remaining)}`,
          style: { width: `${remaining}%` }
        })
      })
    ]
  })
}

function ProviderCard({ provider }) {
  const balance = balanceFromDetails(provider)
  const windows = provider.windows.map(window => displayWindow(provider, window))
  const details = balance === null
    ? provider.details.map(detail => translatedDetail(provider, detail))
    : []
  const hasData = provider.available && (windows.length || details.length || balance !== null)
  const [collapsed, setCollapsed] = useState(() => hasData && collapsedSet().has(provider.provider))
  const open = !hasData || !collapsed
  const summary = cardSummary(windows, balance)
  const toggle = () => {
    if (!hasData) return
    const next = collapsedSet()
    if (next.has(provider.provider)) next.delete(provider.provider)
    else next.add(provider.provider)
    storage?.set(COLLAPSED_KEY, [...next])
    setCollapsed(next.has(provider.provider))
  }
  return jsxs('section', {
    className: 'grid gap-3 rounded-xl border border-(--ui-border) bg-(--ui-surface-raised) p-3',
    children: [
      jsxs('button', {
        type: 'button',
        'aria-expanded': open,
        disabled: !hasData,
        onClick: toggle,
        className: 'flex min-w-0 items-center gap-2 text-left disabled:cursor-default',
        children: [
          jsx(DisclosureCaret, { open, className: hasData ? 'text-(--ui-text-tertiary)' : 'invisible' }),
          jsx(StatusDot, { tone: hasData ? 'good' : provider.refreshing ? 'warn' : 'bad' }),
          jsx('span', { className: 'truncate text-sm font-medium text-foreground', children: provider.label }),
          provider.plan ? jsx('span', { className: 'rounded-full bg-(--ui-control-bg) px-2 py-0.5 text-[0.65rem] text-(--ui-text-secondary)', children: provider.plan }) : null,
          summary && !open ? jsx('span', { className: 'ml-auto shrink-0 tabular-nums text-xs text-(--ui-text-secondary)', children: summary }) : null,
          provider.refreshing ? jsx(GlyphSpinner, { className: `${summary && !open ? '' : 'ml-auto '}size-3 shrink-0 text-(--ui-text-tertiary)` }) : null
        ]
      }),
      open && hasData && windows.length ? jsx('div', {
        className: 'grid gap-3',
        children: windows.map((window, index) => jsx(WindowRow, { window }, `${window.label}-${index}`))
      }) : null,
      open && balance !== null ? jsx(BalanceRow, { balance, ceiling: balanceCeiling(provider) }) : null,
      open && details.length ? jsx('div', {
        className: 'grid gap-1 border-t border-(--ui-border) pt-2 text-xs text-(--ui-text-secondary)',
        children: details.map((detail, index) => jsx('div', { children: detail }, index))
      }) : null,
      !hasData ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: provider.reason || 'sem dados disponíveis' }) : null
    ]
  })
}

function QuotaPane() {
  const [refreshing, setRefreshing] = useState(false)
  const query = useQuery({
    queryKey: ['quota-pane', 'quota'],
    queryFn: () => api('/quota'),
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: 1
  })

  const refresh = async () => {
    setRefreshing(true)
    try {
      await api('/refresh', { method: 'POST' })
      await new Promise(resolve => setTimeout(resolve, 900))
      await query.refetch()
    } finally {
      setRefreshing(false)
    }
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col bg-(--ui-surface)',
    children: [
      jsxs('header', {
        className: 'flex items-center gap-2 border-b border-(--ui-border) px-3 py-2.5',
        children: [
          jsx('div', { className: 'text-sm font-medium', children: 'Cotas e saldos' }),
          jsx('span', { className: 'ml-auto text-[0.6875rem] text-(--ui-text-tertiary)', children: query.data?.refreshing ? 'atualizando' : 'cache 90s' }),
          jsx(Button, { variant: 'ghost', size: 'sm', disabled: refreshing, onClick: () => void refresh(), children: refreshing ? 'Atualizando…' : 'Atualizar' })
        ]
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsx('div', {
          className: 'grid gap-3 p-3',
          children: query.isLoading
            ? jsx('div', { className: 'flex items-center gap-2 py-6 text-sm text-(--ui-text-tertiary)', children: [jsx(GlyphSpinner, { className: 'size-4' }), 'Coletando cotas…'] })
            : query.isError
              ? jsx('div', { className: 'rounded-xl border border-(--ui-danger)/40 p-3 text-sm text-(--ui-danger)', children: `Backend indisponível: ${query.error?.message || 'erro desconhecido'}` })
              : (query.data?.providers || []).map(provider => jsx(ProviderCard, { provider }, provider.provider))
        })
      })
    ]
  })
}

export default {
  id: 'quota-pane',
  name: 'Cotas',
  description: 'Cotas e saldos de Codex, OpenCode Go, Command Code, DeepSeek, Firecrawl, OpenRouter e Parallel em um pane nativo.',
  defaultEnabled: true,
  register(ctx) {
    api = ctx.rest
    storage = ctx.storage
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'Cotas',
      data: { placement: 'right', width: '370px' },
      render: () => jsx(QuotaPane, {})
    })
    queueMicrotask(() => host.revealPane('quota-pane:pane'))
  }
}
