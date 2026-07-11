"""Envío del email diario con novedades y bajadas de precio."""

from __future__ import annotations

import html
import logging
import os
import smtplib
from collections.abc import Sequence
from email.message import EmailMessage
from typing import Any

from src.models import Cambio, Listing, Novedad
from src.scoring import negociacion

log = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Falta alguna variable de entorno necesaria para enviar el email."""


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise ConfigError(f"falta la variable de entorno {name} (mira .env.example)")
    return value


def _miles(n: int) -> str:
    """1234567 -> '1.234.567' (separador de miles a la española)."""
    return f"{n:,}".replace(",", ".")


#: Si un anuncio arrastra varios eventos sin notificar, cuál manda. Una bajada es
#: más accionable que un alta, así que gana ella.
_PRIORIDAD = {Cambio.BAJADA: 0, Cambio.NUEVO: 1}


def _como_novedades(items: Sequence[Listing | Novedad]) -> list[Novedad]:
    """Adaptador: acepta anuncios sueltos y los trata como novedades.

    Existe para que `--force-send` pueda pasar listings crudos sin inventarse un
    histórico. No es una dualidad de tipos permanente: dentro de este módulo,
    todo es Novedad.
    """
    return _agrupar(
        [n if isinstance(n, Novedad) else Novedad(n, Cambio.NUEVO) for n in items]
    )


def _agrupar(novedades: list[Novedad]) -> list[Novedad]:
    """Un anuncio, una tarjeta.

    Un piso puede llegar aquí con dos eventos pendientes a la vez: el alta de
    ayer, que no se llegó a enviar porque falló el SMTP, y la bajada de hoy. Sin
    esto saldría dos veces en el mismo correo, en dos secciones distintas.
    """
    mejor: dict[str, Novedad] = {}
    for n in novedades:
        code = n.listing.property_code
        actual = mejor.get(code)
        if actual is None or _PRIORIDAD.get(n.cambio, 9) < _PRIORIDAD.get(actual.cambio, 9):
            mejor[code] = n
    return list(mejor.values())


# --- render ------------------------------------------------------------------


def _detalles(l: Listing) -> str:
    """Línea de características. Planta y ascensor solo si el tipo los tiene."""
    partes = [l.tipo.capitalize(), f"{l.m2} m²", f"{l.habitaciones} hab", f"{l.banos} baños"]
    if l.tiene_planta:
        partes.append("bajo" if l.planta == 0 else f"planta {l.planta}")
        partes.append("con ascensor" if l.ascensor else "sin ascensor")
    if l.exterior is not None:
        partes.append("exterior" if l.exterior else "interior")
    partes.append(f"{_miles(l.precio_m2)} €/m²")
    return " · ".join(partes)


def _titular(n: Novedad) -> str:
    """Cuánto cuesta y, si ha bajado, cuánto ha bajado."""
    if n.cambio is Cambio.BAJADA and n.precio_anterior:
        return (
            f"{_miles(n.listing.precio)} € "
            f"(antes {_miles(n.precio_anterior)} €, {n.delta_pct:.0f}%)"
        )
    return f"{_miles(n.listing.precio)} €"


def sobre_el_umbral(n: Novedad, config: dict[str, Any]) -> bool:
    """¿Merece este anuncio ocupar sitio en el email?

    El umbral se aplica solo a las novedades. Una bajada de precio pasa siempre:
    es rara, es accionable y es la señal por la que existe todo esto. Filtrarla
    por nota sería tirar lo mejor que tiene el sistema.
    """
    if n.cambio is not Cambio.NUEVO:
        return True
    umbral = (config.get("notificacion") or {}).get("min_score")
    if umbral is None or n.score is None:
        return True
    return n.score.total >= umbral


def _motivos(n: Novedad) -> list[str]:
    """Por qué este anuncio tiene la nota que tiene. Una nota sin motivo no se discute."""
    motivos = []

    if n.dias_vistos is not None and n.dias_vistos >= 45:
        motivos.append(f"lo vemos publicado desde hace {n.dias_vistos} días")

    if margen := negociacion.texto(n.negociacion, n.listing.precio):
        motivos.append(margen)

    if not n.score:
        return motivos

    d = n.score.detalle

    if p := d.get("precio", {}).get("desviacion_pct"):
        muestra = d["precio"].get("muestra", 0)
        fuente = d["precio"].get("comparado_con")
        ref = f"mediana del barrio (n={muestra})" if fuente == "propia" else "referencia de config"
        motivos.append(f"{abs(p):.0f}% {'por debajo' if p < 0 else 'por encima'} de la {ref}")

    if metro := d.get("localizacion", {}).get("metro"):
        motivos.append(f"metro a {metro['metros']} m")
    if parque := d.get("localizacion", {}).get("parque"):
        motivos.append(f"zona verde a {parque['metros']} m")

    if positivas := d.get("texto", {}).get("positivas"):
        motivos.append(", ".join(positivas))
    if negativas := d.get("texto", {}).get("negativas"):
        motivos.append("⚠ " + ", ".join(negativas))

    if n.score.dimensiones < 3:
        motivos.append("nota parcial: faltan datos de alguna dimensión")

    return motivos


def _tarjeta_html(n: Novedad) -> str:
    l = n.listing
    foto = (
        f'<img src="{html.escape(l.foto_url)}" alt="" '
        'style="width:100%;max-width:520px;border-radius:8px;display:block">'
        if l.foto_url
        else ""
    )
    nota = (
        f'<span style="background:#0b5ed7;color:#fff;border-radius:6px;'
        f'padding:2px 8px;font-weight:600">{n.score.total:.0f}</span> '
        if n.score
        else ""
    )
    motivos = "".join(
        f'<li style="margin:2px 0">{html.escape(m)}</li>' for m in _motivos(n)
    )
    return f"""
    <div style="border:1px solid #e2e2e2;border-radius:10px;padding:16px;margin-bottom:16px">
      {foto}
      <h3 style="margin:12px 0 4px">
        {nota}
        <a href="{html.escape(l.url)}" style="color:#0b5ed7;text-decoration:none">
          {html.escape(_titular(n))} · {html.escape(l.barrio)}
        </a>
      </h3>
      <p style="margin:0;color:#555">{html.escape(_detalles(l))}</p>
      <ul style="margin:8px 0 0;padding-left:18px;color:#333">{motivos}</ul>
      <p style="margin:8px 0 0;color:#666;font-size:14px">{html.escape(_resumen(l.descripcion))}</p>
    </div>
    """


def _resumen(descripcion: str, limite: int = 200) -> str:
    if len(descripcion) <= limite:
        return descripcion
    return descripcion[:limite].rsplit(" ", 1)[0] + "…"


def render_html(items: Sequence[Listing | Novedad]) -> str:
    novedades = _como_novedades(items)
    secciones = []

    for cambio, titulo in ((Cambio.BAJADA, "Han bajado de precio"), (Cambio.NUEVO, "Novedades")):
        grupo = [n for n in novedades if n.cambio is cambio]
        if not grupo:
            continue
        grupo.sort(key=lambda n: n.score.total if n.score else 0, reverse=True)
        secciones.append(
            f'<h2 style="margin:24px 0 12px">{titulo} ({len(grupo)})</h2>'
            + "".join(_tarjeta_html(n) for n in grupo)
        )

    return (
        '<div style="font-family:system-ui,sans-serif;max-width:600px;margin:0 auto">'
        + "".join(secciones)
        + "</div>"
    )


def render_text(items: Sequence[Listing | Novedad]) -> str:
    novedades = _como_novedades(items)
    lineas: list[str] = []

    for cambio, titulo in ((Cambio.BAJADA, "HAN BAJADO DE PRECIO"), (Cambio.NUEVO, "NOVEDADES")):
        grupo = [n for n in novedades if n.cambio is cambio]
        if not grupo:
            continue
        grupo.sort(key=lambda n: n.score.total if n.score else 0, reverse=True)
        lineas += [f"{titulo} ({len(grupo)})", ""]
        for n in grupo:
            nota = f"[{n.score.total:.0f}] " if n.score else ""
            lineas += [f"- {nota}{_titular(n)} · {n.listing.barrio}", f"  {_detalles(n.listing)}"]
            lineas += [f"  · {m}" for m in _motivos(n)]
            lineas += [f"  {_resumen(n.listing.descripcion)}", f"  {n.listing.url}", ""]

    return "\n".join(lineas)


# --- envío -------------------------------------------------------------------


def build_message(items: Sequence[Listing | Novedad], config: dict[str, Any]) -> EmailMessage:
    novedades = _como_novedades(items)
    notif = config.get("notificacion") or {}
    plantilla = notif.get("asunto", "{n} novedad(es)")
    asunto = plantilla.format(
        n=len(novedades),
        nuevos=sum(1 for n in novedades if n.cambio is Cambio.NUEVO),
        bajadas=sum(1 for n in novedades if n.cambio is Cambio.BAJADA),
    )

    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = _env("EMAIL_FROM")
    msg["To"] = _env("EMAIL_TO")
    msg.set_content(render_text(novedades))
    msg.add_alternative(render_html(novedades), subtype="html")
    return msg


def send(
    items: Sequence[Listing | Novedad], config: dict[str, Any], dry_run: bool = False
) -> bool:
    """Envía el email. Con dry_run solo lo pinta por consola. True si se envió."""
    notif = config.get("notificacion") or {}
    if not items and notif.get("omitir_si_vacio", True):
        log.info("no hay novedades: no se envía email")
        return False

    if dry_run:
        log.info("[dry-run] email que se habría enviado:\n%s", render_text(items))
        return False

    msg = build_message(items, config)
    _deliver(msg)

    log.info("email enviado a %s con %d novedades", msg["To"], len(items))
    return True


def _connect() -> smtplib.SMTP:
    """Abre la conexión SMTP. El puerto 465 habla SSL directo; el resto, STARTTLS."""
    host = _env("SMTP_HOST")
    port = int(_env("SMTP_PORT", "587"))

    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=30)

    smtp = smtplib.SMTP(host, port, timeout=30)
    smtp.starttls()
    return smtp


def _deliver(msg: EmailMessage) -> None:
    try:
        with _connect() as smtp:
            smtp.login(_env("SMTP_USER"), _env("SMTP_PASSWORD"))
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise ConfigError(
            "el servidor SMTP ha rechazado el usuario/contraseña. "
            "Con Gmail hace falta una contraseña de aplicación de 16 caracteres "
            "(https://myaccount.google.com/apppasswords), no la contraseña de la cuenta. "
            f"Respuesta del servidor: {e.smtp_error!r}"
        ) from e
