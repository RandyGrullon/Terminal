// Settings group "Alertas" (schema-alerts.ts): the keys match the backend defaults (app/settings_ext/alerts.py),
// secrets are of kind secret, the select defaults are valid options and the plain-text fields are validated.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { validateField } from '../src/components/settings/validate'
import {
  GROUPS_EXT,
  MIN_LEVEL_OPTIONS,
  QUIET_TIMES,
  SMTP_SECURITY_OPTIONS,
  validAddress,
  validateAlertField,
} from '../src/components/settings/schema-alerts'

const TEXT_KEYS = ['alerts.telegram_chat_id', 'alerts.smtp_host', 'alerts.smtp_user', 'alerts.email_from', 'alerts.email_to']

declare const __IMP_ALERTS_FRONTEND__: string // set by tests/imp-alerts.run.mjs (the bundle runs from a temp folder)
const backendDefaults = readFileSync(join(__IMP_ALERTS_FRONTEND__, '..', 'backend', 'app', 'settings_ext', 'alerts.py'), 'utf8')
const BACKEND_KEYS = Array.from(backendDefaults.matchAll(/"(alerts\.[a-z_]+)":/g), (m) => m[1]).sort()

test('one "Alertas" group with exactly the backend keys', () => {
  assert.equal(GROUPS_EXT.length, 1)
  const g = GROUPS_EXT[0]
  assert.equal(g.id, 'alerts')
  assert.equal(g.title, 'Alertas')
  const keys = g.fields.map((f) => f.key).sort()
  assert.ok(BACKEND_KEYS.length >= 20)
  assert.deepEqual(keys, BACKEND_KEYS)
  assert.equal(new Set(keys).size, keys.length)
})

test('secrets are secret fields; the password and the plain-text fields have their own kinds', () => {
  const byKey = Object.fromEntries(GROUPS_EXT[0].fields.map((f) => [f.key, f]))
  assert.equal(byKey['alerts.telegram_token'].kind, 'secret')
  assert.equal(byKey['alerts.smtp_password'].kind, 'password')
  for (const key of TEXT_KEYS) {
    assert.ok(byKey[key], key)
    assert.equal(byKey[key].kind, 'text', `${key}: plain text`)
    assert.ok(byKey[key].placeholder, `${key}: placeholder`)
  }
  assert.equal(byKey['alerts.smtp_port'].kind, 'int')
  assert.equal(byKey['alerts.signal_symbols'].kind, 'symbols')
})

test('select defaults are valid options', () => {
  assert.equal(QUIET_TIMES.length, 48)
  assert.ok(QUIET_TIMES.some((o) => o.value === '23:00') && QUIET_TIMES.some((o) => o.value === '07:00'))
  assert.equal(QUIET_TIMES[1].value, '00:30')
  assert.ok(MIN_LEVEL_OPTIONS.some((o) => o.value === 'warn'))
  assert.deepEqual(SMTP_SECURITY_OPTIONS.map((o) => o.value), ['starttls', 'ssl', 'none'])
  assert.match(backendDefaults, /"alerts\.min_level": "warn"/)
  assert.match(backendDefaults, /"alerts\.smtp_security": "starttls"/)
  assert.match(backendDefaults, /"alerts\.quiet_start": "23:00"/)
})

test('validation of the alert fields', () => {
  assert.equal(validateAlertField('gate.take_score', 5), undefined) // not ours: generic rules
  for (const key of [...TEXT_KEYS, 'alerts.telegram_token']) {
    assert.equal(validateAlertField(key, ''), null, `${key} is optional`)
    assert.equal(validateAlertField(key, 5), 'Valor no válido')
  }
  assert.equal(validateAlertField('alerts.telegram_token', '123456789:AAHkQ_fake-token-for-tests-0123456789'), null)
  assert.equal(validateAlertField('alerts.telegram_token', 'bot123456789:AAHkQ_fake-token-for-tests-0123456789'), null)
  assert.match(validateAlertField('alerts.telegram_token', 'mi token') ?? '', /token de Telegram/)
  assert.equal(validateAlertField('alerts.telegram_chat_id', '-1001234567890'), null)
  assert.equal(validateAlertField('alerts.telegram_chat_id', '@mi_canal'), null)
  assert.match(validateAlertField('alerts.telegram_chat_id', 'mi chat') ?? '', /número del chat/)
  assert.equal(validateAlertField('alerts.smtp_host', 'smtp.gmail.com'), null)
  assert.match(validateAlertField('alerts.smtp_host', 'smtp://gmail') ?? '', /nombre del servidor/)
  assert.match(validateAlertField('alerts.smtp_user', 'yo @x.com') ?? '', /espacios/)
  assert.equal(validateAlertField('alerts.email_from', 'Nexus <nexus@pc.lan>'), null)
  assert.match(validateAlertField('alerts.email_from', 'nexus') ?? '', /Dirección no válida/)
  assert.equal(validateAlertField('alerts.email_to', 'a@b.com, c@d.org; e@f.net'), null)
  assert.equal(validateAlertField('alerts.email_to', 'a@b.com, nadie'), 'Dirección no válida: nadie')
  assert.ok(validAddress('yo@example.com') && !validAddress('yo@') && !validAddress('<yo@example.com'))
})

test('the SMTP password may contain spaces (password kind, not the API-key rule); the chat id keyboard is not numeric', () => {
  const byKey = Object.fromEntries(GROUPS_EXT[0].fields.map((f) => [f.key, f]))
  const pwd = byKey['alerts.smtp_password']
  assert.equal(validateAlertField('alerts.smtp_password', 'abcd efgh'), undefined) // no special rule: the generic one applies
  assert.equal(validateField(pwd, 'abcd efgh ijkl mnop'), null)
  assert.equal(validateField(pwd, 'mi frase de paso'), null)
  assert.equal(validateField(pwd, ''), null)
  assert.equal(validateField(pwd, 5), 'Valor no válido')
  // group ids start with '-' and channels with '@': a phone's numeric keypad has neither
  assert.notEqual(byKey['alerts.telegram_chat_id'].inputMode, 'numeric')
})
