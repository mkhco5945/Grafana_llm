import React, { useEffect, useState } from 'react';
import type { CSSProperties } from 'react';

import { config } from '@grafana/runtime';
import { Button } from '@grafana/ui';

import { getConnection, getPluginSettings, savePluginConnection } from './api';
import type { ModelConnection } from './api';

type Defaults = {
  openai_base_url: string;
  openai_model: string;
  has_api_key: boolean;
};

const initialDefaults: Defaults = {
  openai_base_url: 'https://api.openai.com/v1',
  openai_model: '',
  has_api_key: false,
};

const fieldStyle: CSSProperties = {
  display: 'block',
  width: '100%',
  height: 40,
  minHeight: 40,
  boxSizing: 'border-box',
  marginTop: 6,
  padding: '0 10px',
  lineHeight: 'normal',
  fontSize: 14,
  fontFamily: 'inherit',
  color: 'inherit',
  background: 'transparent',
  border: '1px solid rgba(128,128,128,.65)',
  borderRadius: 6,
};

const labelStyle: CSSProperties = {
  display: 'block',
  minWidth: 0,
  fontSize: 13,
  fontWeight: 500,
};

export default function ModelSettings({ value, onChange, disabled }: {
  value: ModelConnection;
  onChange: (value: ModelConnection) => void;
  disabled: boolean;
}) {
  const [defaults, setDefaults] = useState<Defaults>(initialDefaults);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const user = config.bootData.user;
  const colors = config.theme2.colors;
  const canManage = user.isGrafanaAdmin || user.orgRole === 'Admin';

  useEffect(() => {
    let active = true;
    Promise.allSettled([getConnection(), getPluginSettings()]).then(([bridgeResult, grafanaResult]) => {
      if (!active) return;
      const bridge = bridgeResult.status === 'fulfilled' ? bridgeResult.value : null;
      const grafana = grafanaResult.status === 'fulfilled' ? grafanaResult.value : null;
      if (!bridge && !grafana) {
        setError('تنظیمات اتصال از Grafana خوانده نشد. وضعیت سرویس AI را بررسی کنید.');
        setLoaded(true);
        return;
      }

      const jsonData = grafana?.jsonData || {};
      const savedProvider = jsonData.llmProvider;
      const provider: ModelConnection['provider'] =
        savedProvider === 'openai' || savedProvider === 'ollama'
          ? savedProvider
          : bridge?.provider || 'ollama';
      const baseUrl = jsonData.openaiBaseUrl || bridge?.openai_base_url || initialDefaults.openai_base_url;
      const model = jsonData.openaiModel || bridge?.openai_model || '';
      const hasApiKey = bridge?.has_api_key ?? Boolean(grafana?.secureJsonFields?.openaiApiKey);

      setDefaults({ openai_base_url: baseUrl, openai_model: model, has_api_key: hasApiKey });
      onChange({
        provider,
        base_url: provider === 'openai' ? baseUrl : '',
        model: provider === 'openai' ? model : '',
        api_key: '',
      });
      if (!bridge) {
        setError('تنظیمات ذخیره‌شده از Grafana بازیابی شد، اما سرویس AI در دسترس نیست.');
      } else {
        setError('');
      }
      setLoaded(true);
    });
    return () => { active = false; };
  }, [onChange]);

  const save = async () => {
    setError('');
    setNotice('');
    if (value.provider === 'openai' && (!value.base_url.trim() || !value.model.trim())) {
      setError('آدرس API و نام دقیق مدل الزامی است.');
      return;
    }
    if (value.provider === 'openai' && !value.api_key && !defaults.has_api_key) {
      setError('برای ذخیره اتصال OpenAI-compatible کلید API را وارد کنید.');
      return;
    }
    if (
      value.provider === 'openai' && defaults.has_api_key && !value.api_key &&
      value.base_url.replace(/\/+$/, '') !== defaults.openai_base_url.replace(/\/+$/, '')
    ) {
      setError('آدرس API تغییر کرده است؛ کلید مربوط به آدرس جدید را دوباره وارد کنید.');
      return;
    }
    setSaving(true);
    try {
      await savePluginConnection(value);
      const hasApiKey = defaults.has_api_key || Boolean(value.api_key);
      setDefaults({ openai_base_url: value.base_url, openai_model: value.model, has_api_key: hasApiKey });
      onChange({ ...value, api_key: '' });
      setNotice('تنظیمات در Grafana ذخیره شد و بعد از بستن صفحه یا راه‌اندازی مجدد باقی می‌ماند.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  };

  const clearKey = async () => {
    setSaving(true);
    setError('');
    setNotice('');
    try {
      await savePluginConnection({ ...value, api_key: '' }, true);
      setDefaults(current => ({ ...current, has_api_key: false }));
      setNotice('کلید API ذخیره‌شده حذف شد.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  };

  return (
    <fieldset
      dir="rtl"
      disabled={disabled || saving || !canManage}
      style={{ margin: '18px 0', padding: 18, border: `1px solid ${colors.border.medium}`, borderRadius: 9 }}
    >
      <legend style={{ padding: '0 8px', fontWeight: 600 }}>اتصال هوش مصنوعی</legend>

      {!loaded && <p style={{ opacity: 0.7 }}>در حال خواندن تنظیمات ذخیره‌شده…</p>}
      {error && <p role="alert" style={{ color: colors.error.text, margin: '4px 0 12px' }}>{error}</p>}
      {notice && <p role="status" style={{ color: colors.success.text, margin: '4px 0 12px' }}>{notice}</p>}
      {!canManage && <p>فقط مدیر سازمان Grafana می‌تواند این اتصال مشترک را تغییر دهد.</p>}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '14px 16px' }}>
        <label style={labelStyle}>
          ارائه‌دهنده
          <select
            aria-label="ارائه‌دهنده مدل"
            style={{ ...fieldStyle, appearance: 'auto' }}
            value={value.provider}
            onChange={event => {
              const provider = event.target.value as ModelConnection['provider'];
              onChange({
                provider,
                base_url: provider === 'openai' ? defaults.openai_base_url : '',
                model: provider === 'openai' ? defaults.openai_model : '',
                api_key: '',
              });
            }}
          >
            <option value="ollama">Ollama محلی</option>
            <option value="openai">OpenAI / OpenAI-compatible</option>
          </select>
        </label>

        <label style={labelStyle}>
          مدل
          <input
            dir="ltr"
            style={fieldStyle}
            value={value.model}
            placeholder={value.provider === 'ollama' ? 'انتخاب خودکار مدل محلی' : 'نام دقیق مدل در سرویس‌دهنده'}
            onChange={event => onChange({ ...value, model: event.target.value })}
          />
        </label>
      </div>

      {value.provider === 'openai' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '14px 16px', marginTop: 14 }}>
          <label style={labelStyle}>
            آدرس پایه API
            <input
              dir="ltr"
              style={fieldStyle}
              type="url"
              value={value.base_url}
              placeholder="https://api.openai.com/v1"
              onChange={event => onChange({ ...value, base_url: event.target.value, api_key: '' })}
            />
          </label>
          <label style={labelStyle}>
            کلید API
            <input
              dir="ltr"
              style={fieldStyle}
              type="password"
              autoComplete="off"
              value={value.api_key || ''}
              placeholder={defaults.has_api_key ? 'کلید ذخیره شده است؛ برای حفظ آن خالی بگذارید' : 'کلید API را وارد کنید'}
              onChange={event => onChange({ ...value, api_key: event.target.value })}
            />
          </label>
        </div>
      )}

      {value.provider === 'openai' && (
        <div style={{ marginTop: 12, padding: '9px 11px', borderRadius: 6, background: colors.background.secondary, fontSize: 12 }}>
          {defaults.has_api_key
            ? 'کلید API به‌صورت رمزگذاری‌شده در Grafana ذخیره است. خالی بودن کادر بالا عمدی است و مقدار کلید هیچ‌وقت دوباره نمایش داده نمی‌شود.'
            : 'هنوز کلید API ذخیره نشده است. با دکمه «ذخیره اتصال» آن را رمزگذاری و در Grafana ذخیره کنید.'}
        </div>
      )}

      <p style={{ margin: '12px 0 0', fontSize: 12, opacity: 0.72 }}>
        تغییرات روی پیام بعدی اعمال می‌شوند. مدل بیرونی باید Chat Completions و tool calling را پشتیبانی کند.
      </p>

      {canManage && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 14 }}>
          <Button type="button" onClick={() => void save()} disabled={disabled || saving || !loaded}>
            {saving ? 'در حال ذخیره…' : 'ذخیره اتصال'}
          </Button>
          {defaults.has_api_key && (
            <Button type="button" variant="secondary" onClick={() => void clearKey()} disabled={disabled || saving}>
              حذف کلید ذخیره‌شده
            </Button>
          )}
        </div>
      )}
    </fieldset>
  );
}
