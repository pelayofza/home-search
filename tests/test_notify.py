from __future__ import annotations

import pytest

from src import notify


@pytest.fixture
def smtp_env(monkeypatch):
    for key, value in {
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "yo@example.com",
        "SMTP_PASSWORD": "secreto",
        "EMAIL_FROM": "yo@example.com",
        "EMAIL_TO": "yo@example.com",
    }.items():
        monkeypatch.setenv(key, value)


class FakeSMTP:
    """Sustituto de smtplib.SMTP: registra lo que se le pide sin tocar la red."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.login_args = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_miles_formato_espanol():
    assert notify._miles(345_000) == "345.000"


def test_render_text_lleva_precio_y_url(listing_factory):
    texto = notify.render_text([listing_factory(precio=345_000, url="https://x/1")])
    assert "345.000 €" in texto
    assert "https://x/1" in texto


def test_detalles_de_un_piso_incluye_planta_y_ascensor(listing_factory):
    detalles = notify._detalles(listing_factory(tipo="piso", planta=4, ascensor=True))
    assert "planta 4" in detalles
    assert "con ascensor" in detalles
    assert "exterior" in detalles


def test_detalles_de_un_chalet_omite_planta_y_ascensor(listing_factory):
    detalles = notify._detalles(listing_factory(tipo="chalet", planta=0, ascensor=False))
    assert "planta" not in detalles
    assert "ascensor" not in detalles
    assert detalles.startswith("Chalet")


def test_detalles_omite_exterior_si_se_desconoce(listing_factory):
    detalles = notify._detalles(listing_factory(exterior=None))
    assert "exterior" not in detalles
    assert "interior" not in detalles


def test_un_anuncio_con_dos_eventos_sale_una_sola_vez(listing_factory):
    """Si el SMTP falla una noche, al día siguiente el alta y la bajada llegan juntas."""
    from src.models import Cambio, Novedad

    piso = listing_factory(property_code="A", precio=850_000)
    texto = notify.render_text(
        [
            Novedad(piso, Cambio.NUEVO),
            Novedad(piso, Cambio.BAJADA, precio_anterior=900_000),
        ]
    )

    assert texto.count("https://example.com/X-1") == 1
    assert "HAN BAJADO DE PRECIO" in texto
    assert "NOVEDADES" not in texto, "gana la bajada, que es lo accionable"


def test_render_html_escapa_la_descripcion(listing_factory):
    html = notify.render_html([listing_factory(descripcion='<script>alert("x")</script>')])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_conserva_las_comas_del_css(listing_factory):
    assert "system-ui,sans-serif" in notify.render_html([listing_factory()])


def test_build_message_es_multipart(listing_factory, config, smtp_env):
    config["notificacion"] = {"asunto": "{n} vivienda(s) nueva(s)"}
    msg = notify.build_message([listing_factory()], config)

    assert msg["Subject"] == "1 vivienda(s) nueva(s)"
    assert msg["To"] == "yo@example.com"
    assert {p.get_content_type() for p in msg.iter_parts()} == {"text/plain", "text/html"}


def test_falta_variable_de_entorno(listing_factory, config, monkeypatch):
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    with pytest.raises(notify.ConfigError, match="EMAIL_FROM"):
        notify.build_message([listing_factory()], config)


def test_send_envia_por_smtp(listing_factory, config, smtp_env, fake_smtp):
    assert notify.send([listing_factory()], config) is True

    smtp = fake_smtp.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.started_tls is True
    assert smtp.login_args == ("yo@example.com", "secreto")
    assert len(smtp.sent) == 1


def test_puerto_465_usa_ssl_sin_starttls(listing_factory, config, smtp_env, fake_smtp, monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "465")
    notify.send([listing_factory()], config)

    smtp = fake_smtp.instances[0]
    assert smtp.port == 465
    assert smtp.started_tls is False


def test_dry_run_no_toca_smtp(listing_factory, config, smtp_env, fake_smtp):
    assert notify.send([listing_factory()], config, dry_run=True) is False
    assert fake_smtp.instances == []


def test_sin_novedades_no_se_envia(config, smtp_env, fake_smtp):
    config["notificacion"] = {"omitir_si_vacio": True}
    assert notify.send([], config) is False
    assert fake_smtp.instances == []


def test_error_de_auth_se_traduce_a_configerror(listing_factory, config, smtp_env, monkeypatch):
    import smtplib

    class RejectingSMTP(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")

    monkeypatch.setattr(notify.smtplib, "SMTP", RejectingSMTP)

    with pytest.raises(notify.ConfigError, match="contraseña de aplicación"):
        notify.send([listing_factory()], config)
