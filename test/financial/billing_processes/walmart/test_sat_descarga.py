"""
test_sat_descarga.py
--------------------
Pruebas del firmado de _sat_descarga.py. NO llama al SAT y NO usa la e.firma
real.

A) Ida y vuelta (siempre): genera una e.firma ficticia (RSA + certificado
   autofirmado con RFC de prueba), arma los cuatro sobres y verifica cada
   firma con la llave publica: el digest corresponde al nodo firmado y el
   SignatureValue corresponde al SignedInfo.

B) Byte por byte (si SAT_FIXTURES_DIR apunta a los casos de prueba de
   phpcfdi/sat-ws-descarga-masiva, carpeta tests/_files): con la e.firma de
   pruebas del SAT (EKU9003173C9) los sobres tienen que salir IDENTICOS a los
   de esa libreria. RSA-PKCS1v15 es deterministico, asi que digest y firma
   tambien coinciden.

Uso:
  python test_sat_descarga.py
  SAT_FIXTURES_DIR=<ruta a tests/_files> python test_sat_descarga.py
"""

import base64
import hashlib
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.append(_dir)

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

import _sat_descarga as sat

FALLAS = []


def check(nombre, condicion, detalle=''):
    print(f"  {'OK ' if condicion else 'FALLA'} {nombre}{(' -> ' + detalle) if detalle and not condicion else ''}")
    if not condicion:
        FALLAS.append(nombre)


def fiel_ficticia():
    llave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'PRUEBA SA DE CV'),
                        x509.NameAttribute(NameOID.X500_UNIQUE_IDENTIFIER, 'AAA010101AAA / XAXX010101000')])
    ahora = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre)
            .public_key(llave.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(ahora - timedelta(days=1)).not_valid_after(ahora + timedelta(days=30))
            .sign(llave, hashes.SHA256()))
    cer = cert.public_bytes(serialization.Encoding.DER)
    key = llave.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                              serialization.BestAvailableEncryption(b'clave-prueba'))
    return sat.cargar_fiel(cer, key, 'clave-prueba'), cert


def verificar_firma(sobre, nodo_firmado_regex, cert):
    """Recalcula el digest del nodo firmado (tal como se envio, sin la firma,
    con el namespace que declara la referencia) y verifica la firma."""
    m = re.search(r'<Signature xmlns="http://www.w3.org/2000/09/xmldsig#">(<SignedInfo>.*?</SignedInfo>)'
                  r'<SignatureValue>(.*?)</SignatureValue>', sobre)
    signed_info = m.group(1).replace('<SignedInfo>', '<SignedInfo xmlns="http://www.w3.org/2000/09/xmldsig#">')
    digest_doc = re.search(r'<DigestValue>(.*?)</DigestValue>', signed_info).group(1)
    firmado = nodo_firmado_regex(sobre)
    digest = base64.b64encode(hashlib.sha1(firmado.encode()).digest()).decode()
    try:
        cert.public_key().verify(base64.b64decode(m.group(2)), signed_info.encode(),
                                 padding.PKCS1v15(), hashes.SHA1())
        firma_ok = True
    except Exception:
        firma_ok = False
    return digest == digest_doc, firma_ok


def nodo_operacion(sobre):
    """Del sobre recupera <des:Operacion xmlns:des=...><des:nodo ...>extra</des:nodo></des:Operacion>
    quitando la firma: es lo que se firmo."""
    m = re.search(r'<s:Body><des:(\w+)><des:(\w+) ([^>]*)>(.*?)<Signature .*?</Signature></des:\2>', sobre)
    op, nodo, attrs, extra = m.groups()
    return f'<des:{op} xmlns:des="{sat.NS_DES}"><des:{nodo} {attrs}>{extra}</des:{nodo}></des:{op}>'


def nodo_timestamp(sobre):
    m = re.search(r'<u:Timestamp u:Id="_0">(.*?)</u:Timestamp>', sobre)
    return ('<u:Timestamp xmlns:u="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-wssecurity-utility-1.0.xsd" u:Id="_0">{m.group(1)}</u:Timestamp>')


def prueba_ida_y_vuelta():
    print('A) Ida y vuelta con e.firma ficticia')
    fiel, cert = fiel_ficticia()
    check('RFC leido del certificado', fiel['rfc'] == 'AAA010101AAA', fiel['rfc'])
    sobres = {
        'autenticacion': (sat.sobre_autenticacion(fiel), nodo_timestamp),
        'solicitud emitidos': (sat.sobre_solicitud_emitidos(fiel, '2026-10-01T00:00:00', '2026-10-01T23:59:59',
                                                             tipo_comprobante='I', estado_comprobante='Vigente'),
                               nodo_operacion),
        'solicitud folio': (sat.sobre_solicitud_folio(fiel, 'F59B8A75-6492-4AAA-8AFF-9F334FC588A8'), nodo_operacion),
        'verificacion': (sat.sobre_verificacion(fiel, '3f30a4e1-af73-4085-8991-e4d97eef16bd'), nodo_operacion),
        'descarga': (sat.sobre_descarga(fiel, '4e80345d-917f-40bb-a98f-4a73939343c5_01'), nodo_operacion),
    }
    for nombre, (sobre, extractor) in sobres.items():
        try:
            ET.fromstring(sobre)
            bien_formado = True
        except ET.ParseError:
            bien_formado = False
        check(f'{nombre}: XML bien formado', bien_formado)
        digest_ok, firma_ok = verificar_firma(sobre, extractor, cert)
        check(f'{nombre}: digest del nodo firmado', digest_ok)
        check(f'{nombre}: firma RSA-SHA1 valida', firma_ok)
    s = sobres['solicitud emitidos'][0]
    check('emitidos: atributos en orden alfabetico',
          re.search(r'<des:solicitud EstadoComprobante="Vigente" FechaFinal=".*?" FechaInicial=".*?" '
                    r'RfcEmisor="AAA010101AAA" RfcSolicitante="AAA010101AAA" TipoComprobante="I" '
                    r'TipoSolicitud="CFDI">', s) is not None)
    check('folio en minusculas', 'Folio="f59b8a75-6492-4aaa-8aff-9f334fc588a8"' in sobres['solicitud folio'][0])


def normalizar_fixture(texto):
    texto = re.sub(r'<\?xml[^>]*\?>\s*', '', texto)
    return sat.sin_espacios(texto).strip()


def prueba_contra_referencia(carpeta):
    print(f'B) Byte por byte contra phpcfdi ({carpeta})')
    leer = lambda p: open(os.path.join(carpeta, p), 'rb').read()
    fiel = cargar_fiel_sin_vigencia(leer('fake-fiel/EKU9003173C9.cer'), leer('fake-fiel/EKU9003173C9.key'),
                                    leer('fake-fiel/EKU9003173C9-password.txt').decode().strip())
    casos = {
        'authenticate/request.xml': sat.sobre_autenticacion(
            fiel, datetime(2019, 8, 1, 3, 38, 19, tzinfo=timezone.utc),
            'uuid-cf6c80fb-00ae-44c0-af56-54ec65decbaa-1'),
        'query/request-issued.xml': sat.sobre_solicitud_emitidos(
            fiel, '2019-01-01T00:00:00', '2019-01-01T00:04:00', tipo_solicitud='Metadata',
            estado_comprobante='Todos'),
        'query/request-item.xml': sat.sobre_solicitud_folio(fiel, '96623061-61fe-49de-b298-c7156476aa8b'),
        'verify/request.xml': sat.sobre_verificacion(fiel, '3f30a4e1-af73-4085-8991-e4d97eef16bd'),
        'download/request.xml': sat.sobre_descarga(fiel, '4e80345d-917f-40bb-a98f-4a73939343c5_01'),
    }
    # Los archivos de la referencia escriben los elementos vacios como <X/> y su
    # propia prueba compara despues de reformatear; el codigo de la referencia
    # (y el nuestro) envia <X></X>. Por eso se compara en forma canonica (C14N),
    # y ademas el DigestValue y el SignatureValue como texto literal.
    for archivo, mio in casos.items():
        esperado = normalizar_fixture(leer(archivo).decode('utf-8'))
        canon_mio = ET.canonicalize(xml_data=mio)
        canon_ref = ET.canonicalize(xml_data=esperado)
        if canon_mio == canon_ref:
            check(f'{archivo}: identico (C14N)', True)
        else:
            i = next((k for k, (a, b) in enumerate(zip(canon_mio, canon_ref)) if a != b),
                     min(len(canon_mio), len(canon_ref)))
            check(f'{archivo}: identico (C14N)', False,
                  f'difiere en {i}: mio ...{canon_mio[max(0, i - 60):i + 60]}... | '
                  f'ref ...{canon_ref[max(0, i - 60):i + 60]}...')
        for etiqueta in ('DigestValue', 'SignatureValue'):
            a = re.search(f'<{etiqueta}>(.*?)</{etiqueta}>', mio).group(1)
            b = re.search(f'<{etiqueta}>(.*?)</{etiqueta}>', esperado).group(1)
            check(f'{archivo}: {etiqueta} igual', a == b)


def cargar_fiel_sin_vigencia(cer, key, password):
    """La e.firma de pruebas del SAT puede estar vencida: para comparar el
    formato se omite solo la revision de vigencia."""
    cert = x509.load_der_x509_certificate(cer)
    llave = serialization.load_der_private_key(key, password=password.encode())
    return {'rfc': sat._rfc_del_certificado(cert), 'cert_b64': base64.b64encode(cer).decode(),
            'serie': str(cert.serial_number), 'emisor': sat._emisor(cert),
            'vence': cert.not_valid_after_utc, 'llave': llave}


def prueba_respuestas(carpeta):
    print('C) Lectura de respuestas de ejemplo')
    leer = lambda p: open(os.path.join(carpeta, p), 'rb').read()
    res = ET.fromstring(leer('verify/response-2-packages.xml'))
    a = sat._atributos(sat._nodo(res, 'VerificaSolicitudDescargaResult'))
    check('verificacion: EstadoSolicitud=3', a.get('EstadoSolicitud') == '3')
    paquetes = [p.text for p in res.iter() if p.tag.endswith('IdsPaquetes')]
    check('verificacion: 2 paquetes', len(paquetes) == 2)
    res = ET.fromstring(leer('download/response-with-package.xml'))
    paquete = sat._nodo(res, 'Paquete')
    zip_bytes = base64.b64decode(paquete.text)
    check('descarga: el paquete es un ZIP', zip_bytes[:2] == b'PK')
    res = ET.fromstring(leer('authenticate/response-with-token.xml'))
    check('autenticacion: token', bool((sat._nodo(res, 'AutenticaResult').text or '').strip()))


if __name__ == '__main__':
    prueba_ida_y_vuelta()
    carpeta = os.getenv('SAT_FIXTURES_DIR', '').strip()
    if carpeta:
        prueba_contra_referencia(carpeta)
        prueba_respuestas(carpeta)
    else:
        print('B y C omitidas: SAT_FIXTURES_DIR vacio.')
    print(f"\n{'TODO OK' if not FALLAS else str(len(FALLAS)) + ' FALLA(S): ' + ', '.join(FALLAS)}")
    sys.exit(1 if FALLAS else 0)
