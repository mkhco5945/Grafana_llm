import React, { useEffect, useState } from 'react';
import { config } from '@grafana/runtime';
import { Button } from '@grafana/ui';
import { getConnection, savePluginConnection } from './api';
import type { ModelConnection } from './api';

export default function ModelSettings({ value, onChange, disabled }: {
  value: ModelConnection; onChange: (value: ModelConnection) => void; disabled: boolean;
}) {
  const [defaults, setDefaults] = useState({ openai_base_url: 'https://api.openai.com/v1', openai_model: '', has_api_key: false });
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const user = config.bootData.user;
  const canManage = user.isGrafanaAdmin || user.orgRole === 'Admin';
  useEffect(() => {
    let active = true;
    getConnection().then(result => {
      if (!active) return;
      setDefaults(result);
      onChange({ provider: result.provider, base_url: result.base_url,
        model: result.provider === 'ollama' ? '' : result.model, api_key: '' });
    }).catch(() => { if (active) setError('Could not load connection defaults. Check the AI bridge.'); });
    return () => { active = false; };
  }, [onChange]);
  const fieldStyle = { display: 'block', width: '100%', padding: 8, marginTop: 4, marginBottom: 12,
    color: 'inherit', background: 'transparent', border: '1px solid #888', borderRadius: 4 };
  const save = async () => {
    setError('');
    setNotice('');
    if (value.provider === 'openai' && (!value.base_url.trim() || !value.model.trim())) {
      setError('API base URL and model are required.');
      return;
    }
    if (value.provider === 'openai' && !value.api_key && !defaults.has_api_key) {
      setError('Enter an API key before saving the OpenAI connection.');
      return;
    }
    if (value.provider === 'openai' && defaults.has_api_key && !value.api_key &&
        value.base_url.replace(/\/+$/, '') !== defaults.openai_base_url.replace(/\/+$/, '')) {
      setError('The API endpoint changed. Enter its API key again so the saved key is never forwarded to a different provider.');
      return;
    }
    setSaving(true);
    try {
      await savePluginConnection(value);
      setDefaults(current => ({ ...current, openai_base_url: value.base_url,
        openai_model: value.model, has_api_key: current.has_api_key || Boolean(value.api_key) }));
      onChange({ ...value, api_key: '' });
      setNotice('Connection saved in Grafana. It will remain available after navigation or reload.');
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
      setNotice('Saved API key removed.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  };
  return <fieldset disabled={disabled || saving || !canManage} style={{ margin: '16px 0', padding: 16, border: '1px solid #888', borderRadius: 6 }}>
    <legend>AI connection</legend>
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!canManage && <p>Only a Grafana organization administrator can change this shared connection. You can use the saved connection.</p>}
    <label>Provider<select style={fieldStyle} value={value.provider} onChange={event => {
      const provider = event.target.value as ModelConnection['provider'];
      onChange({ provider, base_url: provider === 'openai' ? defaults.openai_base_url : '',
        model: provider === 'openai' ? defaults.openai_model : '', api_key: '' });
    }}>
      <option value="ollama">Local Ollama</option>
      <option value="openai">OpenAI / OpenAI-compatible API</option>
    </select></label>
    {value.provider === 'openai' && <>
      <label>API base URL<input style={fieldStyle} type="url" value={value.base_url}
        placeholder="https://api.openai.com/v1" onChange={event => onChange({ ...value, base_url: event.target.value, api_key: '' })} /></label>
      <label>API key<input style={fieldStyle} type="password" autoComplete="off" value={value.api_key || ''}
        placeholder={defaults.has_api_key && value.base_url === defaults.openai_base_url ? 'Server key configured; leave blank to use it' : 'Enter API key'}
        onChange={event => onChange({ ...value, api_key: event.target.value })} /></label>
      <p>The key stays in memory for this page and active requests. It is not saved in chat history or browser storage.
        After reloading, enter it again, or configure OPENAI_API_KEY on the server.</p>
      <p>Your prompts and queried monitoring data will be sent to the selected provider.</p>
    </>}
    <label>Model<input style={fieldStyle} value={value.model} placeholder={value.provider === 'ollama' ? 'Automatic (4b / 8b)' : 'Exact model ID from your provider'}
      onChange={event => onChange({ ...value, model: event.target.value })} /></label>
    <small>Changes apply to your next message or Retry. External models must support Chat Completions with tool calling.</small>
    {canManage && <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
      <Button type="button" onClick={() => void save()} disabled={disabled || saving}>Save connection</Button>
      {defaults.has_api_key && <Button type="button" variant="secondary" onClick={() => void clearKey()} disabled={disabled || saving}>Remove saved API key</Button>}
    </div>}
  </fieldset>;
}
