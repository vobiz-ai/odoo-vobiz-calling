import { Component, onWillUnmount, useEffect, useState } from "@odoo/owl";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const KEYS = [
    ["1", ""], ["2", "ABC"], ["3", "DEF"],
    ["4", "GHI"], ["5", "JKL"], ["6", "MNO"],
    ["7", "PQRS"], ["8", "TUV"], ["9", "WXYZ"],
    ["*", ""], ["0", "+"], ["#", ""],
];

export class VobizSoftphone extends Component {
    static components = { Dropdown };
    static props = [];
    static template = "vobiz_calling.Softphone";

    setup() {
        this.phone = useService("vobiz_softphone");
        this.state = useState(this.phone.state);
        this.form = useState({
            tab: "token",
            ownAccount: false,
            authId: "",
            authToken: "",
            sipUsername: "",
            sipPassword: "",
            number: "",
            callKeypad: false,
        });
        this.keys = KEYS;
        this.clock = useState({ now: Date.now() });
        this.dropdown = useDropdownState();
        const stop = this.phone.onEvent((event) => {
            if (event.type === "incoming" || event.type === "open") {
                this.dropdown.open();
            }
        });
        const tick = setInterval(() => {
            if (this.state.call && this.state.call.connectedAt) {
                this.clock.now = Date.now();
            }
        }, 1000);
        // An endpoint signed in before this page loaded: ask for its password.
        useEffect(
            (pending) => {
                if (pending) {
                    this.form.tab = "sip";
                    this.form.sipUsername = pending;
                }
            },
            () => [this.state.pendingSip]
        );
        // A call's keypad closes with the call.
        useEffect(
            (call) => {
                if (!call) {
                    this.form.callKeypad = false;
                }
            },
            () => [this.state.call]
        );
        onWillUnmount(() => {
            stop();
            clearInterval(tick);
        });
    }

    get badgeClass() {
        if (this.state.phase === "incoming") {
            return "o_vobiz_badge_ringing";
        }
        if (this.state.phase !== "idle") {
            return "o_vobiz_badge_busy";
        }
        return this.state.registered ? "o_vobiz_badge_ready" : "o_vobiz_badge_off";
    }

    get pill() {
        if (this.state.phase === "incoming") {
            return { kind: "ringing", text: _t("Ringing") };
        }
        if (this.state.phase !== "idle") {
            return { kind: "busy", text: _t("On a call") };
        }
        if (!this.state.signedIn) {
            return { kind: "off", text: _t("Signed out") };
        }
        return this.state.registered ? { kind: "ready", text: _t("Ready") } : { kind: "wait", text: _t("Connecting") };
    }

    /** The pill and the call card already say "Ready", "On a call" and "Incoming call";
     * the line below the header is for news. */
    get showStatus() {
        const ringing = this.state.phase === "incoming" || this.state.phase === "answering";
        return Boolean(this.state.status) && this.state.statusKind !== "success" && !ringing;
    }

    get accountLabel() {
        if (this.state.mode === "sip") {
            return _t("SIP endpoint · %s", this.state.identity);
        }
        if (this.state.mode === "company") {
            return _t("Company account · %s", this.state.authId);
        }
        return _t("Your account · %s", this.state.authId);
    }

    get callTime() {
        const call = this.state.call;
        if (!call || !call.connectedAt) {
            return "";
        }
        const seconds = Math.max(0, Math.floor((this.clock.now - call.connectedAt) / 1000));
        return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
    }

    initials(party) {
        const name = ((party && party.name) || "").trim();
        if (!name) {
            return "";
        }
        return name
            .split(/\s+/)
            .slice(0, 2)
            .map((word) => word[0].toUpperCase())
            .join("");
    }

    async signInCompany() {
        await this.phone.signIn("company");
    }

    async signInOwn() {
        const ok = await this.phone.signIn("own", this.form.authId.trim(), this.form.authToken.trim());
        // The Auth Token is only ever needed for the sign-in itself.
        this.form.authToken = "";
        if (ok) {
            this.form.ownAccount = false;
        }
    }

    async signInSip() {
        const password = this.form.sipPassword;
        // The phone keeps the password for its registration; the form does not.
        this.form.sipPassword = "";
        await this.phone.signInSip(this.form.sipUsername.trim(), password);
    }

    onSipKeydown(ev) {
        if (ev.key === "Enter" && this.form.sipUsername && this.form.sipPassword) {
            this.signInSip();
        }
    }

    press(key) {
        this.form.number = `${this.form.number}${key}`;
    }

    backspace() {
        this.form.number = this.form.number.slice(0, -1);
    }

    dial() {
        if (this.phone.call(this.form.number)) {
            this.form.number = "";
        }
    }

    onNumberKeydown(ev) {
        if (ev.key === "Enter") {
            this.dial();
        }
    }

    onCallerIdChange(ev) {
        this.phone.setCallerId(ev.target.value);
    }
}

registry.category("systray").add("vobiz_calling.softphone", { Component: VobizSoftphone }, { sequence: 90 });
