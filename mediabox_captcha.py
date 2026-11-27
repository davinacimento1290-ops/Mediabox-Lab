"""CAPTCHA local gratuito com desafio de uso único.
O gabarito nunca é enviado nem armazenado na sessão do navegador.
A resposta é validada por HMAC com a chave secreta do Flask.
"""
import hashlib
import hmac
import secrets
import time

_SECRET = None
TTL = 180


def configurar_chave(chave):
    global _SECRET
    _SECRET = str(chave).encode("utf-8")


def emitir_captcha(sessao):
    if not _SECRET:
        raise RuntimeError("A chave de segurança do CAPTCHA não foi configurada.")

    a = secrets.randbelow(9) + 2
    b = secrets.randbelow(9) + 2
    if secrets.randbelow(2):
        pergunta, resposta = f"{a} + {b} = ?", str(a + b)
    else:
        maior, menor = max(a, b), min(a, b)
        pergunta, resposta = f"{maior} - {menor} = ?", str(maior - menor)

    criado = int(time.time())
    nonce = secrets.token_urlsafe(18)
    assinatura = hmac.new(
        _SECRET, f"{criado}:{nonce}:{resposta}".encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    token = f"{criado}.{nonce}.{assinatura}"
    sessao["mediabox_captcha"] = {
        "token": token,
        "expires": criado + TTL,
    }
    return pergunta, token


def verificar_captcha(sessao, token, resposta):
    desafio = sessao.pop("mediabox_captcha", None)
    if not isinstance(desafio, dict):
        return False, "Verificação ausente ou já utilizada. Gere um novo desafio."

    token = str(token or "")
    esperado = str(desafio.get("token", ""))
    if not token or not hmac.compare_digest(esperado, token):
        return False, "Desafio inválido. Gere uma nova verificação."

    try:
        criado_txt, nonce, assinatura = token.split(".", 2)
        criado = int(criado_txt)
        expirado = (
            int(time.time()) > int(desafio.get("expires", 0))
            or int(time.time()) - criado > TTL
            or criado > int(time.time()) + 30
        )
    except (TypeError, ValueError):
        return False, "Desafio inválido. Gere uma nova verificação."

    if expirado:
        return False, "O desafio expirou. Gere uma nova verificação."

    resposta = str(resposta or "").strip()
    assinatura_calculada = hmac.new(
        _SECRET, f"{criado}:{nonce}:{resposta}".encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(assinatura, assinatura_calculada):
        return False, "Resposta anti-robô incorreta. Gere um novo desafio."
    return True, ""
