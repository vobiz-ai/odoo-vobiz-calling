import { reactive } from "@odoo/owl";
import { loadJS } from "@web/core/assets";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";

/**
 * The Vobiz softphone: one JsSIP user agent per signed-in browser tab.
 *
 * Two ways in. With an access token (the company's account, or the agent's own
 * Auth ID and Auth Token), the browser registers as `<sub>_<auth_id>` with a
 * short-lived JWT the Odoo server has Vobiz mint, and never holds an Auth
 * Token. With a SIP endpoint, it registers as the endpoint with the password
 * the agent typed here; that password stays in this page and is never sent to
 * Odoo. Odoo's web client is a single page, so the phone, and a call in
 * progress, survive moving between Odoo screens; a reload ends a call.
 */

const JSSIP_URL = "/vobiz_calling/static/lib/jssip/jssip.min.js";

// Without STUN the SDP carries host candidates only and Vobiz cannot reach the
// browser's media. These are the values Vobiz's own SDK uses.
const PC_CONFIG = { iceServers: [{ urls: ["stun:stun.l.google.com:19302"] }] };

// JsSIP holds an INVITE, and the 200 OK answering one, until ICE gathering
// finishes. On a machine with several network interfaces that took longer than
// an inbound call's ring time. Send as soon as STUN answers, after 2.5 s at most.
const ICE_CAP_MS = 2500;

// No space, deliberately. Vobiz copies the registration's User-Agent into the
// address it routes <Dial><User> through, and JsSIP's default ("JsSIP 3.10.1")
// makes that address unparseable: the caller hears ringback and the browser
// never rings.
const USER_AGENT = "VobizOdooCalling/1.0.0";

const TOKEN_RETRY_DELAYS_MS = [5000, 10000, 20000, 40000];
// A SIP endpoint's sign-in has no token to renew: ask Odoo this often whether
// it still stands (an administrator may have stopped the endpoint's use).
const SIP_CHECK_MS = 3 * 60 * 1000;
const JWT_ERRORS = {
    10001: "INVALID_ACCESS_TOKEN",
    10005: "ACCESS_TOKEN_NOT_VALID_YET",
    10006: "ACCESS_TOKEN_EXPIRED",
    10007: "INVALID_ACCESS_TOKEN_SIGNATURE",
    10010: "MAX_ALLOWED_LOGIN_REACHED",
};

export const softphoneService = {
    dependencies: ["bus_service"],

    start(env, { bus_service }) {
        const state = reactive({
            loaded: false,
            signedIn: false,
            companyAccount: false,
            mode: null,
            authId: "",
            identity: "",
            numbers: [],
            callerId: "",
            registered: false,
            // idle | incoming | answering | calling | ringing | in_call
            phase: "idle",
            status: "",
            statusKind: "muted",
            incoming: null, // { number, name }
            call: null, // { direction, number, name, connectedAt, muted, held }
            busy: false,
            // A SIP endpoint signed in from another tab, or before a reload: its
            // password is not in this page, so the panel asks for it again.
            pendingSip: "",
        });
        const listeners = new Set();

        let ua = null;
        let session = null;
        let sipDomain = "registrar.vobiz.ai";
        let registrar = "";
        let accessToken = "";
        let accessTokenExp = 0;
        // A SIP endpoint's password: only here, in this page, while signed in.
        let sipPassword = "";
        let renewalTimer = null;
        let renewalFailures = 0;
        let notYetValidRetries = 0;
        let forcedRenewalTried = false;
        let audioEl = null;
        let ring = null;
        let pendingCallRef = null;
        let answeredElsewhere = false;
        // The identity this tab registered as, so a sign-out meant for an older
        // sign-in never stops a newer one.
        let currentIdentity = "";
        const callerNames = new Map();

        function setStatus(text, kind = "muted") {
            state.status = text;
            state.statusKind = kind;
        }

        function emit(event) {
            for (const listener of listeners) {
                try {
                    listener(event);
                } catch (err) {
                    console.warn("[Vobiz]", err);
                }
            }
        }

        function applyState(data) {
            state.signedIn = Boolean(data.signedIn);
            state.companyAccount = Boolean(data.companyAccount);
            state.mode = data.mode || null;
            state.authId = data.authId || "";
            state.identity = data.signedIn ? data.identity || "" : "";
            state.numbers = data.numbers || [];
            state.callerId = data.callerId || "";
            if (data.sipDomain) {
                sipDomain = data.sipDomain;
            }
            if (data.registrar) {
                registrar = data.registrar;
            }
            state.loaded = true;
        }

        // -- signing in --------------------------------------------------

        async function load() {
            try {
                const data = await rpc("/vobiz/panel/state", {});
                if (data.signedIn && data.mode === "sip" && !sipPassword) {
                    // Signed in with an endpoint before this page loaded. Its
                    // password was never stored: ask for it, and leave a sign-in
                    // another tab may still be using alone.
                    applyState({ ...data, signedIn: false });
                    state.pendingSip = data.identity || "";
                    setStatus(_t("Enter the SIP endpoint's password to use the phone in this tab."), "warning");
                    return;
                }
                applyState(data);
                if (data.signedIn) {
                    await startPhone(data);
                }
            } catch (err) {
                state.loaded = true;
                setStatus(errorText(err), "danger");
            }
        }

        async function signIn(mode, authId = "", authToken = "") {
            state.busy = true;
            setStatus(_t("Signing in…"));
            try {
                const data = await rpc("/vobiz/panel/sign_in", {
                    mode,
                    auth_id: authId,
                    auth_token: authToken,
                });
                sipPassword = "";
                state.pendingSip = "";
                applyState(data);
                await startPhone(data);
                return true;
            } catch (err) {
                setStatus(errorText(err), "danger");
                return false;
            } finally {
                state.busy = false;
            }
        }

        /** Sign in as a SIP endpoint. Only the username goes to Odoo. */
        async function signInSip(username, password) {
            if (!username || !password) {
                return false;
            }
            state.busy = true;
            setStatus(_t("Signing in…"));
            try {
                const data = await rpc("/vobiz/panel/sign_in", { mode: "sip", sip_username: username });
                sipPassword = password;
                state.pendingSip = "";
                applyState(data);
                await startPhone(data);
                return true;
            } catch (err) {
                setStatus(errorText(err), "danger");
                return false;
            } finally {
                state.busy = false;
            }
        }

        async function signOut() {
            stopPhone();
            sipPassword = "";
            state.pendingSip = "";
            try {
                applyState(await rpc("/vobiz/panel/sign_out", {}));
            } catch (err) {
                console.warn("[Vobiz] sign-out:", err);
            }
            setStatus(_t("Signed out."));
        }

        async function setCallerId(number) {
            try {
                applyState({ ...(await rpc("/vobiz/panel/caller_id", { number })), sipDomain, registrar });
            } catch (err) {
                setStatus(errorText(err), "danger");
            }
        }

        // -- the SIP user agent ----------------------------------------------

        async function startPhone(data) {
            stopPhone();
            try {
                await loadJS(JSSIP_URL);
            } catch {
                setStatus(_t("Could not load the phone. Reload the page and try again."), "danger");
                return;
            }
            const viaSip = data.mode === "sip";
            accessToken = viaSip ? "" : data.accessToken;
            accessTokenExp = viaSip ? 0 : Number(data.accessTokenExpiresAt) || 0;
            currentIdentity = data.identity || "";
            const socket = new window.JsSIP.WebSocketInterface(data.registrar || registrar);
            const agent = new window.JsSIP.UA({
                sockets: [socket],
                uri: `sip:${data.identity}@${data.sipDomain || sipDomain}`,
                // A SIP endpoint answers the registrar's digest challenge itself.
                ...(viaSip ? { authorization_user: data.identity, password: sipPassword } : {}),
                register: true,
                user_agent: USER_AGENT,
                // Vobiz's media server refuses JsSIP's session-timer proposal with
                // "422 Session Interval Too Small", which leaves no CDR at all.
                session_timers: false,
            });
            ua = agent;
            const current = () => ua === agent;
            if (!viaSip) {
                // The registrar authenticates an access token's REGISTER by this header alone.
                agent.registrator().setExtraHeaders([`X-Vobiz-Jwt: ${accessToken}`]);
            }
            notYetValidRetries = 0;
            forcedRenewalTried = false;
            setStatus(_t("Connecting…"));

            agent.on("registered", () => {
                if (!current()) {
                    return;
                }
                notYetValidRetries = 0;
                forcedRenewalTried = false;
                state.registered = true;
                if (state.phase === "idle") {
                    setStatus(_t("Ready"), "success");
                }
            });
            agent.on("registrationFailed", (e) => {
                if (!current()) {
                    return;
                }
                state.registered = false;
                if (viaSip) {
                    const cause = (e && e.cause) || "";
                    if (!["Authentication Error", "Rejected", "Not Found"].includes(cause)) {
                        // A network failure: JsSIP registers again once it reconnects.
                        setStatus(_t("The phone could not connect: %s", cause || _t("unknown")), "warning");
                        return;
                    }
                    // A wrong endpoint username or password: nothing to retry.
                    stopPhone();
                    sipPassword = "";
                    rpc("/vobiz/panel/sign_out", {})
                        .then((out) => applyState(out))
                        .catch(() => applyState({ signedIn: false, companyAccount: state.companyAccount }));
                    setStatus(_t("Vobiz refused this SIP endpoint's username or password."), "danger");
                    return;
                }
                const code = jwtErrorCode(e);
                if (code === 10005 && notYetValidRetries < 3) {
                    // The registrar's clock is behind: the same token works in a moment.
                    notYetValidRetries += 1;
                    setTimeout(() => current() && agent.register(), 3000);
                    return;
                }
                if (code !== null && code !== 10005 && !forcedRenewalTried) {
                    forcedRenewalTried = true;
                    renewToken({ force: true });
                    return;
                }
                const why = code !== null ? `${JWT_ERRORS[code] || "Unauthorised"} (${code})` : (e && e.cause) || "unknown";
                setStatus(_t("The phone could not connect: %s", why), "danger");
            });
            agent.on("unregistered", () => {
                if (current()) {
                    state.registered = false;
                    setStatus(_t("Not connected. Reconnecting…"), "warning");
                }
            });
            agent.on("disconnected", () => {
                if (current()) {
                    state.registered = false;
                    setStatus(_t("Lost the connection to Vobiz. Reconnecting…"), "warning");
                }
            });
            agent.on("newRTCSession", (event) => {
                if (current() && event.originator === "remote") {
                    ringIn(event.session);
                }
            });
            agent.start();
            if (viaSip) {
                scheduleSipCheck();
            } else {
                scheduleRenewal();
            }
        }

        function stopPhone() {
            clearTimeout(renewalTimer);
            renewalTimer = null;
            stopRingtone();
            if (session) {
                try {
                    session.terminate();
                } catch {
                    // already gone
                }
                session = null;
            }
            if (ua) {
                const old = ua;
                ua = null;
                try {
                    old.stop();
                } catch {
                    // nothing more to do on the way out
                }
            }
            state.registered = false;
            state.phase = "idle";
            state.incoming = null;
            state.call = null;
        }

        function scheduleRenewal(delayMs) {
            clearTimeout(renewalTimer);
            const due = (accessTokenExp - 60) * 1000 - Date.now();
            renewalTimer = setTimeout(() => renewToken(), delayMs ?? Math.max(5000, due));
        }

        /** A SIP endpoint's sign-in: stop the phone once Odoo has ended it. */
        function scheduleSipCheck() {
            clearTimeout(renewalTimer);
            renewalTimer = setTimeout(async () => {
                const agent = ua;
                let data;
                try {
                    data = await rpc("/vobiz/panel/token", {});
                } catch {
                    if (ua === agent) {
                        scheduleSipCheck();
                    }
                    return;
                }
                if (ua !== agent) {
                    return;
                }
                if (!data.signedIn || data.identity !== currentIdentity) {
                    if (session) {
                        // A call in progress finishes first.
                        scheduleSipCheck();
                        return;
                    }
                    stopPhone();
                    sipPassword = "";
                    applyState({ signedIn: false, companyAccount: state.companyAccount });
                    setStatus(_t("Signed out: you signed in again somewhere else, or the sign-in ended."), "warning");
                    return;
                }
                scheduleSipCheck();
            }, SIP_CHECK_MS);
        }

        /**
         * A new token, presented at once with a REGISTER; a call in progress is
         * its own dialog and is not touched. A sign-in that has ended (signed out,
         * replaced in another tab, or expired) stops the phone.
         */
        async function renewToken({ force = false } = {}) {
            const agent = ua;
            if (!agent) {
                return;
            }
            let data;
            try {
                data = await rpc("/vobiz/panel/token", { force });
            } catch (err) {
                if (ua !== agent) {
                    return;
                }
                if (Date.now() < accessTokenExp * 1000) {
                    const delay = TOKEN_RETRY_DELAYS_MS[Math.min(renewalFailures, TOKEN_RETRY_DELAYS_MS.length - 1)];
                    renewalFailures += 1;
                    scheduleRenewal(delay);
                }
                return;
            }
            if (ua !== agent) {
                return;
            }
            if (!data.signedIn) {
                stopPhone();
                applyState({ signedIn: false, companyAccount: state.companyAccount });
                setStatus(_t("Signed out: you signed in again somewhere else, or the sign-in expired."), "warning");
                return;
            }
            renewalFailures = 0;
            accessToken = data.accessToken;
            accessTokenExp = Number(data.accessTokenExpiresAt) || 0;
            agent.registrator().setExtraHeaders([`X-Vobiz-Jwt: ${accessToken}`]);
            agent.register();
            scheduleRenewal();
        }

        // -- calls ----------------------------------------------------------------

        function ringIn(incoming) {
            // One call at a time: a second caller is turned away as busy.
            if (session) {
                try {
                    incoming.terminate({ status_code: 486, reason_phrase: "Busy Here" });
                } catch {
                    // already gone
                }
                return;
            }
            session = incoming;
            answeredElsewhere = false;
            const identity = incoming.remote_identity || {};
            const number = digitsOf(identity.display_name) || digitsOf(identity.uri && identity.uri.user);
            state.incoming = { number, name: callerNames.get(number) || "" };
            state.phase = "incoming";
            setStatus(_t("Incoming call"), "info");
            attachAudio(incoming);
            followHold(incoming);
            startRingtone();
            emit({ type: "incoming" });

            incoming.on("confirmed", () => {
                if (session === incoming) {
                    state.phase = "in_call";
                    state.call = {
                        direction: "inbound",
                        number: state.incoming?.number || "",
                        name: state.incoming?.name || "",
                        connectedAt: Date.now(),
                        muted: false,
                        held: false,
                    };
                    state.incoming = null;
                    setStatus(_t("On a call"), "success");
                }
            });
            incoming.on("ended", () => endCall(incoming, _t("Call ended.")));
            incoming.on("failed", (e) => {
                const cause = (e && e.cause) || "";
                let text = _t("The call ended: %s", cause || _t("unknown"));
                if (cause === "Canceled") {
                    text = answeredElsewhere ? _t("Another agent answered the call.") : _t("Missed call.");
                } else if (cause === "Rejected") {
                    text = _t("Declined.");
                } else if (cause === "User Denied Media Access") {
                    text = _t("Could not answer: the browser refused the microphone.");
                }
                endCall(incoming, text);
            });
        }

        function accept() {
            if (!session || state.phase !== "incoming") {
                return;
            }
            stopRingtone();
            state.phase = "answering";
            setStatus(_t("Connecting the call…"), "info");
            try {
                capIceGathering(session);
                session.answer({
                    mediaConstraints: { audio: true, video: false },
                    pcConfig: PC_CONFIG,
                    sessionTimersExpires: 300,
                });
            } catch (err) {
                setStatus(_t("Could not answer: %s", err.message || err), "danger");
                try {
                    session.terminate();
                } catch {
                    // already gone
                }
            }
        }

        function decline() {
            if (!session || state.phase !== "incoming") {
                return;
            }
            const declined = session;
            try {
                declined.terminate({ status_code: 486, reason_phrase: "Busy Here" });
            } catch {
                endCall(declined, _t("Declined."));
            }
        }

        function hangup() {
            if (!session) {
                return;
            }
            try {
                session.terminate();
            } catch (err) {
                console.warn("[Vobiz] hang-up:", err);
            }
        }

        /**
         * Place a call with this browser as the A leg. Odoo's /vobiz/answer then
         * puts the browser in a private room and dials the customer into it.
         */
        function call(rawNumber, name = "") {
            const number = String(rawNumber || "").replace(/[^\d+]/g, "");
            if (!number) {
                return false;
            }
            if (!ua || !state.registered) {
                setStatus(_t("Sign in to the phone first."), "warning");
                emit({ type: "open" });
                return false;
            }
            if (session) {
                setStatus(_t("Finish the current call first."), "warning");
                emit({ type: "open" });
                return false;
            }
            pendingCallRef = randomHex(12);
            const headers = [`X-VH-Call-Ref: ${pendingCallRef}`];
            if (accessToken) {
                headers.unshift(`X-Vobiz-Jwt: ${accessToken}`);
            }
            if (state.callerId) {
                headers.push(`X-VH-Caller-ID: ${state.callerId}`);
            }
            let outgoing;
            try {
                outgoing = ua.call(`sip:${number}@${sipDomain}`, {
                    mediaConstraints: { audio: true, video: false },
                    pcConfig: PC_CONFIG,
                    sessionTimersExpires: 300,
                    extraHeaders: headers,
                });
            } catch (err) {
                setStatus(
                    err && err.name === "NotAllowedError"
                        ? _t("The browser refused the microphone.")
                        : _t("Could not start the call: %s", err.message || err),
                    "danger"
                );
                return false;
            }
            session = outgoing;
            capIceGathering(outgoing);
            attachAudio(outgoing);
            followHold(outgoing);
            state.phase = "calling";
            state.call = {
                direction: "outbound",
                number,
                name,
                connectedAt: null,
                callRef: pendingCallRef,
                muted: false,
                held: false,
            };
            setStatus(_t("Calling %s…", number), "info");
            emit({ type: "open" });
            // Vobiz answers this leg as soon as it is in the room, before the
            // customer's phone has even rung: "confirmed" means "your side is up".
            // The server says when the customer joins.
            outgoing.on("confirmed", () => {
                if (session === outgoing && state.phase === "calling") {
                    state.phase = "ringing";
                    setStatus(_t("Ringing %s…", number), "info");
                }
            });
            outgoing.on("ended", () => endCall(outgoing, _t("Call ended.")));
            outgoing.on("failed", (e) => {
                // Vobiz answers this leg as soon as Odoo puts it in a room. Refused
                // before that, the call never got through to Odoo, and when Odoo
                // cannot be reached Vobiz says "Busy": find out, and say which.
                const beforeRoom = session === outgoing && state.phase === "calling";
                endCall(outgoing, _t("The call failed: %s", (e && e.cause) || _t("unknown")));
                if (beforeRoom && e && e.originator === "remote") {
                    explainRefusedCall();
                }
            });
            return true;
        }

        async function explainRefusedCall() {
            let data;
            try {
                data = await rpc("/vobiz/panel/check_reach", {});
            } catch {
                return;
            }
            if (data && data.reached === false && !session && state.phase === "idle") {
                setStatus(
                    _t(
                        "Vobiz could not reach this Odoo at %s (%s), so the call could not start. An administrator should check the Public URL for Vobiz in Settings.",
                        data.url,
                        data.why
                    ),
                    "danger"
                );
            }
        }

        // -- in-call controls: the same JsSIP calls Vobiz's browser SDK makes ----

        function toggleMute() {
            if (!session || !state.call || state.phase !== "in_call") {
                return;
            }
            const muted = !state.call.muted;
            try {
                if (muted) {
                    session.mute({ audio: true });
                } else {
                    session.unmute({ audio: true });
                }
                state.call.muted = muted;
            } catch (err) {
                console.warn("[Vobiz] mute:", err);
            }
        }

        /** Hold with a re-INVITE, as Vobiz's SDK does; the hold/unhold events set the state. */
        function toggleHold() {
            if (!session || !state.call || state.phase !== "in_call") {
                return;
            }
            let started = false;
            try {
                started = state.call.held ? session.unhold() : session.hold();
            } catch (err) {
                console.warn("[Vobiz] hold:", err);
            }
            if (!started) {
                setStatus(_t("Could not change the hold now. Try again in a moment."), "warning");
            }
        }

        function followHold(rtcSession) {
            rtcSession.on("hold", ({ originator }) => {
                if (session === rtcSession && state.call && originator === "local") {
                    state.call.held = true;
                    setStatus(_t("On hold"), "warning");
                }
            });
            rtcSession.on("unhold", ({ originator }) => {
                if (session === rtcSession && state.call && originator === "local") {
                    state.call.held = false;
                    setStatus(_t("On a call"), "success");
                }
            });
        }

        /** A keypad tone to the other side, for menus that ask for one. */
        function sendDigit(digit) {
            if (!session || state.phase !== "in_call" || !/^[0-9*#]$/.test(String(digit))) {
                return;
            }
            try {
                session.sendDTMF(String(digit));
            } catch (err) {
                console.warn("[Vobiz] keypad tone:", err);
            }
        }

        function endCall(ended, text) {
            if (session !== ended) {
                return;
            }
            session = null;
            stopRingtone();
            state.phase = "idle";
            state.incoming = null;
            state.call = null;
            setStatus(text, "muted");
        }

        // -- live updates from the server ------------------------------------

        bus_service.subscribe("vobiz/event", (payload) => {
            if (!payload) {
                return;
            }
            if (payload.type === "incoming" && payload.from) {
                const number = digitsOf(payload.from);
                callerNames.set(number, payload.partnerName || "");
                if (state.incoming && state.incoming.number === number) {
                    state.incoming.name = payload.partnerName || "";
                }
            } else if (payload.type === "answeredElsewhere") {
                answeredElsewhere = true;
            } else if (payload.type === "signedOut") {
                // The server ended this sign-in (the account or this Odoo's address
                // changed). A call in progress finishes first; the next token
                // renewal then stops the phone.
                if (state.signedIn && payload.identity === currentIdentity && !state.call && !state.incoming) {
                    stopPhone();
                    sipPassword = "";
                    applyState({ signedIn: false, companyAccount: state.companyAccount });
                    setStatus(payload.message || _t("Signed out."), "warning");
                }
            } else if (payload.type === "outbound" && state.call && payload.callRef === state.call.callRef) {
                if (payload.state === "connected") {
                    state.phase = "in_call";
                    state.call.connectedAt = Date.now();
                    setStatus(_t("On a call"), "success");
                } else if (payload.state === "ended" && !state.call.connectedAt) {
                    const outcome = {
                        no_answer: _t("No answer."),
                        busy: _t("Busy."),
                        canceled: _t("Call ended."),
                    }[payload.outcome] || _t("The call could not be connected.");
                    setStatus(outcome, "muted");
                }
            }
        });

        // -- media, ringtone, ICE -------------------------------------------------

        function attachAudio(rtcSession) {
            if (!audioEl) {
                audioEl = document.createElement("audio");
                audioEl.autoplay = true;
                audioEl.hidden = true;
                document.body.appendChild(audioEl);
            }
            const bind = (pc) => {
                if (!pc) {
                    return;
                }
                pc.addEventListener("track", (event) => {
                    audioEl.srcObject = event.streams[0];
                    audioEl.play().catch((err) => console.warn("[Vobiz] audio:", err));
                });
            };
            rtcSession.on("peerconnection", (event) => bind(event.peerconnection));
            bind(rtcSession.connection);
        }

        function capIceGathering(rtcSession) {
            let settled = false;
            let timedOut = false;
            let lastReady = null;
            const go = () => {
                if (settled || !lastReady) {
                    return;
                }
                settled = true;
                try {
                    lastReady();
                } catch {
                    // gathering already finished on its own
                }
            };
            rtcSession.on("icecandidate", ({ candidate, ready }) => {
                lastReady = ready;
                const line = (candidate && candidate.candidate) || "";
                if ((candidate && candidate.type === "srflx") || line.includes(" typ srflx") || timedOut) {
                    go();
                }
            });
            setTimeout(() => {
                timedOut = true;
                go();
            }, ICE_CAP_MS);
        }

        function startRingtone() {
            stopRingtone();
            try {
                const Ctx = window.AudioContext || window.webkitAudioContext;
                if (!Ctx) {
                    return;
                }
                const ctx = new Ctx();
                const beep = () => {
                    const osc = ctx.createOscillator();
                    const gain = ctx.createGain();
                    osc.frequency.value = 440;
                    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
                    gain.gain.exponentialRampToValueAtTime(0.12, ctx.currentTime + 0.02);
                    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.9);
                    osc.connect(gain).connect(ctx.destination);
                    osc.start();
                    osc.stop(ctx.currentTime + 0.95);
                };
                beep();
                ring = { ctx, timer: setInterval(beep, 2000) };
            } catch (err) {
                // A silent ring is better than a failed call; the panel still shows it.
                console.warn("[Vobiz] ringtone:", err);
            }
        }

        function stopRingtone() {
            if (ring) {
                clearInterval(ring.timer);
                ring.ctx.close().catch(() => {});
                ring = null;
            }
        }

        window.addEventListener("beforeunload", () => {
            try {
                if (ua) {
                    ua.stop();
                }
            } catch {
                // leaving anyway
            }
        });

        load();

        return {
            state,
            signIn,
            signInSip,
            signOut,
            setCallerId,
            call,
            accept,
            decline,
            hangup,
            toggleMute,
            toggleHold,
            sendDigit,
            /** Be told when the panel should open (a call rings, or one is placed). */
            onEvent(listener) {
                listeners.add(listener);
                return () => listeners.delete(listener);
            },
        };
    },
};

function jwtErrorCode(e) {
    const response = e && e.response;
    if (!response || typeof response.getHeader !== "function") {
        return null;
    }
    const code = parseInt(String(response.getHeader("X-vobiz-Jwt-Error-Code") || "").trim(), 10);
    return Number.isFinite(code) ? code : null;
}

function digitsOf(value) {
    const cleaned = String(value || "").replace(/[^\d+]/g, "");
    return cleaned.length > 16 ? cleaned.slice(0, 16) : cleaned;
}

function randomHex(bytes) {
    const buf = new Uint8Array(bytes);
    crypto.getRandomValues(buf);
    return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

function errorText(err) {
    return (err && err.data && err.data.message) || (err && err.message) || _t("Something went wrong.");
}

registry.category("services").add("vobiz_softphone", softphoneService);
