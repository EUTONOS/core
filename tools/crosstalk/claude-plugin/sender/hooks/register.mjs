// Crosstalk P0 throwaway restricted sender. Loaded ONLY through `claude -p /crosstalk-p0-inline
// --plugin-dir <this plugin>` by the tool helper (never installed). Zero model turns: the command
// validates one env payload and makes at most one native $.session.send to an exact session ID.
// Pattern from the tool4/create3 qualification probes.
const TAG = 'crosstalk.p0.sender.v1'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const LETTER = /^CTP0-[0-9a-f]{12}$/
const MAX_PAYLOAD_BYTES = 32768
let attempted = false
const result = (status, extra = {}) => ({ text: JSON.stringify({ tag: TAG, status, ...extra }) })

export function register(on) {
  on('session.start', async ($, event, next) => {
    await $.command.register({
      name: 'crosstalk-p0-inline',
      description: 'Crosstalk P0: send one letter to one exact live Claude session (no model turn)',
    })
    return next(event)
  })

  on('command.run', { command: 'crosstalk-p0-inline' }, async ($) => {
    if (attempted) return result('not-attempted', { sent: false, reason: 'already-attempted' })
    attempted = true
    let payload
    let version = null
    try {
      if (await $.env.get('CROSSTALK_P0_ROLE') !== 'sender') throw Error('role-not-sender')
      const raw = await $.env.get('CROSSTALK_P0_PAYLOAD')
      if (typeof raw !== 'string' || new TextEncoder().encode(raw).length > MAX_PAYLOAD_BYTES) throw Error('payload-bound')
      payload = JSON.parse(raw)
      if (payload?.tag !== TAG || !UUID.test(payload.caller) || !UUID.test(payload.target) ||
          !LETTER.test(payload.letter) || typeof payload.text !== 'string' ||
          !payload.text.startsWith('Crosstalk message from ') ||
          !payload.text.endsWith(`\nref ${payload.letter}`)) throw Error('invalid-payload')
      const ownId = await $.session.id()
      if (ownId !== payload.caller || ownId === payload.target) throw Error('caller-mismatch')
      version = (await $.session.version())?.version ?? null
    } catch (error) {
      return result('not-attempted', { sent: false, reason: String(error?.message ?? 'preflight-failed').slice(0, 100) })
    }
    try {
      const native = await $.session.send({ to: { sessionId: payload.target }, text: payload.text })
      return result(native?.isDelivered === true ? 'queued' : 'refused', {
        sent: native?.isDelivered === true, isDelivered: native?.isDelivered === true, letter: payload.letter, version,
        reason: typeof native?.reason === 'string' ? native.reason.slice(0, 100) : '',
      })
    } catch (error) {
      return result('uncertain', { sent: null, letter: payload.letter, version,
        reason: String(error?.message ?? 'native-outcome-unavailable').slice(0, 100) })
    }
  })
}
