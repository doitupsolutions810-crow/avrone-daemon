"""Credential shapes known by their vendor prefix, shared by every redactor (round 152).

AVRONE had three lists (ctxspawn.redact on every CodeX event, research_note, env_report), and
each knew a different subset. The event redactor knew none of the vendor tokens below, so a
Stripe, Hugging Face, Groq, GitLab, npm, Google OAuth, SendGrid, Slack app, Telegram bot, AWS
temporary (ASIA) or Azure storage key reached run logs, bridges and peers unmasked, and
secret_scan reported the run CLEAN.

Each alternative is anchored on a fixed prefix plus a long body, so ordinary identifiers (hf_cache,
npm_config, SG.items) do not match. PREFIX_GATE lets callers skip the regex for text that holds
none of the prefixes, which keeps the per-event cost to a few substring tests."""
from __future__ import annotations

import re

TOKEN_PREFIX_RE = re.compile(
    r"\b(?:(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{30,}"
    r"|npm_[A-Za-z0-9]{36,}"
    r"|gsk_[A-Za-z0-9]{40,}"
    r"|r8_[A-Za-z0-9]{30,}"
    r"|pplx-[A-Za-z0-9]{40,}"
    r"|xapp-\d-[A-Za-z0-9-]{20,}"
    r"|ya29\.[A-Za-z0-9_-]{20,}"
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r"|ASIA[0-9A-Z]{16}(?![0-9A-Za-z])"
    r"|AccountKey=[A-Za-z0-9+/]{40,}={0,2}"
    r"|\d{8,10}:AA[A-Za-z0-9_-]{30,}"
    # round 156
    r"|dckr_pat_[A-Za-z0-9_-]{20,}"
    r"|shp(?:at|ss|ca|pa)_[a-fA-F0-9]{32}"
    r"|sq0(?:atp|csp)-[A-Za-z0-9_-]{22,}"
    r"|glsa_[A-Za-z0-9]{32}_[a-fA-F0-9]{8}"
    r"|glc_[A-Za-z0-9+/]{32,}={0,2}"
    r"|lin_api_[A-Za-z0-9]{40,}"
    r"|pypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{50,}"
    r"|do[opr]_v1_[a-f0-9]{64}"
    r"|tskey-[a-z]+-[A-Za-z0-9-]{20,}"
    r"|vercel_blob_rw_[A-Za-z0-9_]{20,}"
    r"|EAA[A-Za-z0-9]{32,}"
    r"|key-[a-f0-9]{32}(?![0-9A-Za-z])"
    r"|rzp_(?:live|test)_[A-Za-z0-9]{14,}"
    r"|figd_[A-Za-z0-9_-]{40,}"
    r"|ntn_[A-Za-z0-9]{40,}"
    r"|secret_[A-Za-z0-9]{43}(?![0-9A-Za-z_])"
    r"|FLWSECK(?:_TEST)?-[a-fA-F0-9]{32}-X"
    r"|AGE-SECRET-KEY-1[0-9A-Z]{58}"
    r"|hv[sb]\.[A-Za-z0-9_-]{24,}"
    r"|pat-(?:na|eu)\d-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|AT[AC]TT3[A-Za-z0-9_=-]{20,}"
    r"|sbp_[a-f0-9]{40}"
    r"|SK[0-9a-fA-F]{32}(?![0-9A-Za-z]))")

PREFIX_GATE = ("sk_", "rk_", "pk_", "glpat-", "hf_", "npm_", "gsk_", "r8_", "pplx-", "xapp-", "ya29.",
               "SG.", "ASIA", "AccountKey=", ":AA",
               # round 156
               "dckr_pat_", "shp", "sq0", "glsa_", "glc_", "lin_api_", "pypi-", "_v1_", "tskey-",
               "vercel_blob_rw_", "EAA", "key-", "rzp_", "figd_", "ntn_", "secret_", "FLWSECK", "AGE-SECRET-KEY-",
               "hvs.", "hvb.", "pat-", "TT3", "sbp_", "SK")


# Round 153: secrets known by the words around them rather than by their own shape. Each rule is
# (regex, replacement, any-of gate, secret_scan name). The value groups stop at whitespace, quotes
# and backslashes, so a rule applied to a raw JSON line never eats the string's closing quote
# or a "\n" escape.
_V = r"[^\s\"'\\]+"
# start of a word, also right after a JSON escape ("...\n--password x" in a raw event line)
_START = r"(?:(?<=\\[nrt])|(?<![\w-]))"
CONTEXT_RULES: list[tuple["re.Pattern[str]", str, tuple[str, ...], str]] = [
    # curl -u user:pass / --user user:pass
    (re.compile(_START + r"(-u|--user)(\s+|=)([^\s:\"'\\]+):" + _V),
     r"\1\2\3:[REDACTED]", ("-u", "--user"), "cli-user-password"),
    # --password VALUE (the "=" form is the key=value rule's)
    (re.compile(r"(?i)" + _START + r"(--(?:password|passwd|pass|token|secret|api-?key|access-?token|auth-?token))"
                r"(\s+)(?!-)" + _V),
     r"\1\2[REDACTED]", ("--",), "cli-secret-flag"),
    # mysql/mariadb -pSECRET, sshpass -p SECRET
    (re.compile(r"\b((?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n\"]{0,200}?\s-p)" + _V),
     r"\1[REDACTED]", ("mysql", "mariadb"), "cli-db-password"),
    (re.compile(r"\b(sshpass\s+-p\s*)" + _V), r"\1[REDACTED]", ("sshpass",), "sshpass"),
    # Cookie / Set-Cookie header values
    (re.compile(r"(?i)" + _START + r"((?:set-)?cookie\s*:\s*)[^\r\n\"\\]+"), r"\1[REDACTED]", ("ookie", "OOKIE"), "cookie"),
    # <password>x</password> and friends
    (re.compile(r"(?i)(<(password|passwd|secret|token|api_?key|access_?token|client_?secret)>)[^<]{1,4096}(</\2>)"),
     r"\1[REDACTED]\3", ("</",), "xml-secret"),
    # Round 156. Signed-URL and OAuth query values: ?sig= (Azure SAS), X-Amz-Signature=,
    # X-Goog-Signature=, ?code= (OAuth authorization codes), ?key=, ?client_secret= ...
    # The value stops at & and #, so the rest of the URL stays readable.
    (re.compile(r"(?i)([?&](?:sig|signature|x-amz-signature|x-amz-security-token|x-goog-signature|code|key|apikey"
                r"|api_key|token|auth|client_secret|password|pwd|sas)=)[^&#\s\"'\\]+"),
     r"\1[REDACTED]", ("=",), "url-query-secret"),
    # Incoming-webhook URLs: the path is the credential
    (re.compile(r"(hooks\.slack\.com/(?:services|workflows|triggers)/)[A-Za-z0-9/_-]{8,}"),
     r"\1[REDACTED]", ("hooks.slack.com",), "slack-webhook"),
    (re.compile(r"((?:discord|discordapp)\.com/api/webhooks/\d+/)[A-Za-z0-9_-]{20,}"),
     r"\1[REDACTED]", ("/webhooks/",), "discord-webhook"),
    # Discord bot tokens: base64 user id . 6 . 27+ (no fixed prefix)
    (re.compile(r"(?<![A-Za-z0-9_.-])[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,40}(?![A-Za-z0-9_-])"),
     "[REDACTED]", (".",), "discord-token"),
]
