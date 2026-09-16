import { Button, GlyphSpinner, PANES_AREA, ScrollArea, StatusDot, useQuery } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let api = null

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
  if (remaining <= 10) return 'bg-(--ui-danger)'
  if (remaining <= 25) return 'bg-(--ui-warning)'
  return 'bg-(--ui-success)'
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

function ProviderCard({ provider }) {
  const hasData = provider.available && (provider.windows.length || provider.details.length)
  return jsxs('section', {
    className: 'grid gap-3 rounded-xl border border-(--ui-border) bg-(--ui-surface-raised) p-3',
    children: [
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-2',
        children: [
          jsx(StatusDot, { status: hasData ? 'success' : provider.refreshing ? 'pending' : 'error' }),
          jsx('div', { className: 'truncate text-sm font-medium', children: provider.label }),
          provider.plan ? jsx('span', { className: 'rounded-full bg-(--ui-control-bg) px-2 py-0.5 text-[0.65rem] text-(--ui-text-secondary)', children: provider.plan }) : null,
          provider.refreshing ? jsx(GlyphSpinner, { className: 'ml-auto size-3 text-(--ui-text-tertiary)' }) : null
        ]
      }),
      hasData && provider.windows.length ? jsx('div', {
        className: 'grid gap-3',
        children: provider.windows.map((window, index) => jsx(WindowRow, { window }, `${window.label}-${index}`))
      }) : null,
      provider.details.length ? jsx('div', {
        className: 'grid gap-1 border-t border-(--ui-border) pt-2 text-xs text-(--ui-text-secondary)',
        children: provider.details.map((detail, index) => jsx('div', { children: detail }, index))
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
  description: 'Cotas e saldos de Codex, OpenCode Go, Command Code, DeepSeek e OpenRouter em um pane nativo.',
  defaultEnabled: true,
  register(ctx) {
    api = ctx.rest
    ctx.register({
      id: 'pane',
      area: PANES_AREA,
      title: 'Cotas',
      data: { placement: 'right', width: '370px' },
      render: () => jsx(QuotaPane, {})
    })
  }
}
