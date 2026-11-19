"""
_sat_descarga.py
----------------
Cliente del web service de DESCARGA MASIVA del SAT (v1.5), firmado con la
e.firma (FIEL) de la empresa. Solo funciones; ningun estado global.

Flujo del SAT (cuatro servicios SOAP):
  1. Autenticacion  -> token de ~5 min. El sobre lleva un Timestamp firmado
                       (WS-Security) y el certificado como BinarySecurityToken.
  2. Solicitud      -> SolicitaDescargaEmitidos (por rango de fechas) o
                       SolicitaDescargaFolio (un UUID). Devuelve IdSolicitud.
  3. Verificacion   -> EstadoSolicitud 1 aceptada, 2 en proceso, 3 terminada
                       (trae IdsPaquetes), 4 error, 5 rechazada, 6 vencida.
  4. Descarga       -> cada paquete es un ZIP en base64 con los XML.
La solicitud tarda de minutos a horas: lo que se pide en una corrida se
descarga en otra.

FIRMA (XMLDSig enveloped, RSA-SHA1)
  Igual que la implementacion de referencia phpcfdi/sat-ws-descarga-masiva:
  el XML se arma como texto exacto, con atributos en orden alfabetico y sin
  espacios entre etiquetas. Asi el texto que se firma ya es su forma
  canonica y no hace falta una libreria de canonicalizacion (lxml).
    digest      = base64(SHA1(nodo sin espacios))
    SignedInfo  = texto fijo con el digest
    firma       = base64(RSA-PKCS1v15-SHA1(SignedInfo, llave FIEL))
  Verificado byte por byte contra los casos de prueba de esa libreria (e.firma
  de pruebas del SAT EKU9003173C9): ver test_sat_descarga.py.

E.FIRMA. Se lee de variables de entorno, en este orden:
  SAT_FIEL_CER_PATH / SAT_FIEL_KEY_PATH   rutas a .cer y .key (local)
  SAT_FIEL_CER_B64  / SAT_FIEL_KEY_B64    contenido en base64 (secrets de Kestra)
  SAT_FIEL_PASSWORD                        contrasena de la llave
Nunca se escribe en disco ni en el log.

Unica dependencia fuera de la libreria estandar: cryptography (leer la llave
cifrada PKCS#8 y firmar).
"""

import base64
import hashlib
import os
import re
import urllib.error
import urllib.request
import uuid as uuidlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

URL_AUTENTICACION = 'https://cfdidescargamasivasolicitud.clouda.sat.gob.mx/Autenticacion/Autenticacion.svc'
URL_SOLICITUD = 'https://cfdidescargamasivasolicitud.clouda.sat.gob.mx/SolicitaDescargaService.svc'
URL_VERIFICACION = 'https://cfdidescargamasivasolicitud.clouda.sat.gob.mx/VerificaSolicitudDescargaService.svc'
URL_DESCARGA = 'https://cfdidescargamasiva.clouda.sat.gob.mx/DescargaMasivaService.svc'

ACCION_AUTENTICA = 'http://DescargaMasivaTerceros.gob.mx/IAutenticacion/Autentica'
ACCION_SOLICITUD = 'http://DescargaMasivaTerceros.sat.gob.mx/ISolicitaDescargaService/'
ACCION_VERIFICA = ('http://DescargaMasivaTerceros.sat.gob.mx/IVerificaSolicitudDescargaService/'
                   'VerificaSolicitudDescarga')
ACCION_DESCARGA = 'http://DescargaMasivaTerceros.sat.gob.mx/IDescargaMasivaTercerosService/Descargar'

NS_DES = 'http://DescargaMasivaTerceros.sat.gob.mx'
NS_SOBRE = ('xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:des="{NS_DES}" xmlns:xd="http://www.w3.org/2000/09/xmldsig#"')
X509V3 = 'http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3'

TIMEOUT = int(os.getenv('SAT_TIMEOUT_SEG', '120'))

# Nombres cortos del emisor del certificado, en el formato que espera el SAT
# (el de la implementacion de referencia). Solo informativo: el KeyInfo no
# entra en la firma.
_OID_CORTO = {
    '2.5.4.3': 'CN', '2.5.4.10': 'O', '2.5.4.11': 'OU', '1.2.840.113549.1.9.1': 'emailAddress',
    '2.5.4.9': 'street', '2.5.4.17': 'postalCode', '2.5.4.6': 'C', '2.5.4.8': 'ST',
    '2.5.4.7': 'L', '2.5.4.45': 'x500UniqueIdentifier', '1.2.840.113549.1.9.2': 'unstructuredName',
    '2.5.4.5': 'serialNumber', '2.5.4.41': 'name',
}

# Estados de VerificaSolicitudDescarga
ESTADO_SOLICITUD = {1: 'ACEPTADA', 2: 'EN_PROCESO', 3: 'TERMINADA', 4: 'ERROR',
                    5: 'RECHAZADA', 6: 'VENCIDA'}


class ErrorSAT(Exception):
    """El SAT respondio con error (HTTP o SOAP Fault)."""


# =============================================================================
# E.FIRMA
# =============================================================================

def _leer_bytes(var_ruta, var_b64):
    ruta = os.getenv(var_ruta, '').strip()
    if ruta:
        with open(ruta, 'rb') as fh:
            return fh.read()
    contenido = os.getenv(var_b64, '').strip()
    if contenido:
        return base64.b64decode(contenido)
    return None


def fiel_configurada():
    return bool((os.getenv('SAT_FIEL_CER_PATH') or os.getenv('SAT_FIEL_CER_B64'))
                and (os.getenv('SAT_FIEL_KEY_PATH') or os.getenv('SAT_FIEL_KEY_B64'))
                and os.getenv('SAT_FIEL_PASSWORD'))


def _rfc_del_certificado(cert):
    """El RFC va en x500UniqueIdentifier (2.5.4.45). En una persona moral trae
    'RFC_EMPRESA / RFC_REPRESENTANTE': el primero es el de la empresa."""
    for attr in cert.subject:
        if attr.oid.dotted_string == '2.5.4.45':
            return str(attr.value).split('/')[0].strip().upper()
    return None


def _emisor(cert):
    partes = []
    for rdn in cert.issuer.rdns:
        for attr in rdn:
            nombre = _OID_CORTO.get(attr.oid.dotted_string, attr.oid.dotted_string)
            partes.append(f"{nombre}={attr.value}")
    return ','.join(partes)


def cargar_fiel(cer_bytes=None, key_bytes=None, password=None):
    """Lee y valida la e.firma. Devuelve un dict con lo necesario para firmar.
    Lanza si falta, si la contrasena no abre la llave, si la llave no es del
    certificado o si el certificado no esta vigente."""
    cer_bytes = cer_bytes or _leer_bytes('SAT_FIEL_CER_PATH', 'SAT_FIEL_CER_B64')
    key_bytes = key_bytes or _leer_bytes('SAT_FIEL_KEY_PATH', 'SAT_FIEL_KEY_B64')
    password = password if password is not None else os.getenv('SAT_FIEL_PASSWORD', '')
    if not cer_bytes or not key_bytes or not password:
        raise ErrorSAT('e.firma no configurada (SAT_FIEL_CER_*, SAT_FIEL_KEY_*, SAT_FIEL_PASSWORD).')

    cert = x509.load_der_x509_certificate(cer_bytes)
    try:
        llave = serialization.load_der_private_key(key_bytes, password=password.encode('utf-8'))
    except ValueError:
        raise ErrorSAT('La contrasena no abre la llave privada de la e.firma.')
    if llave.public_key().public_numbers() != cert.public_key().public_numbers():
        raise ErrorSAT('La llave privada no corresponde al certificado de la e.firma.')

    ahora = datetime.now(timezone.utc)
    if not (cert.not_valid_before_utc <= ahora <= cert.not_valid_after_utc):
        raise ErrorSAT(f"Certificado de la e.firma fuera de vigencia "
                       f"({cert.not_valid_before_utc:%Y-%m-%d} a {cert.not_valid_after_utc:%Y-%m-%d}).")

    return {
        'rfc': _rfc_del_certificado(cert),
        'cert_b64': base64.b64encode(cer_bytes).decode('ascii'),
        'serie': str(cert.serial_number),
        'emisor': _emisor(cert),
        'vence': cert.not_valid_after_utc,
        'llave': llave,
    }


# =============================================================================
# FIRMA
# =============================================================================

def sin_espacios(texto):
    """Quita la sangria y los saltos de linea (Helpers::nospaces de la
    referencia): el texto que se firma y el que se envia son identicos."""
    texto = re.sub(r'^[ \t]*', '', texto, flags=re.M)
    return re.sub(r'[ \t]*\r?\n', '', texto)


def _xml(valor):
    return escape(str(valor), {'"': '&quot;'})


def _signed_info(digest, uri=''):
    return sin_espacios(f"""
        <SignedInfo xmlns="http://www.w3.org/2000/09/xmldsig#">
        <CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"></CanonicalizationMethod>
        <SignatureMethod Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1"></SignatureMethod>
        <Reference URI="{uri}">
        <Transforms>
        <Transform Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"></Transform>
        </Transforms>
        <DigestMethod Algorithm="http://www.w3.org/2000/09/xmldsig#sha1"></DigestMethod>
        <DigestValue>{digest}</DigestValue>
        </Reference>
        </SignedInfo>
        """)


def _key_info_x509(fiel):
    return (f"<KeyInfo><X509Data><X509IssuerSerial>"
            f"<X509IssuerName>{_xml(fiel['emisor'])}</X509IssuerName>"
            f"<X509SerialNumber>{fiel['serie']}</X509SerialNumber>"
            f"</X509IssuerSerial><X509Certificate>{fiel['cert_b64']}</X509Certificate>"
            f"</X509Data></KeyInfo>")


def crear_firma(fiel, a_firmar, uri='', key_info=None):
    """Nodo <Signature> para el texto a_firmar (ya con sus namespaces)."""
    digest = base64.b64encode(hashlib.sha1(sin_espacios(a_firmar).encode('utf-8')).digest()).decode()
    signed_info = _signed_info(digest, uri)
    valor = base64.b64encode(fiel['llave'].sign(signed_info.encode('utf-8'),
                                                padding.PKCS1v15(), hashes.SHA1())).decode()
    signed_info = signed_info.replace('<SignedInfo xmlns="http://www.w3.org/2000/09/xmldsig#">',
                                      '<SignedInfo>')
    return (f'<Signature xmlns="http://www.w3.org/2000/09/xmldsig#">{signed_info}'
            f'<SignatureValue>{valor}</SignatureValue>{key_info or _key_info_x509(fiel)}</Signature>')


# =============================================================================
# SOBRES SOAP
# =============================================================================

def _fecha_sat(dt):
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def sobre_autenticacion(fiel, creado=None, token_id=None):
    creado = creado or datetime.now(timezone.utc)
    expira = creado + timedelta(minutes=5)
    token_id = token_id or f"uuid-{uuidlib.uuid4()}-1"
    marca = (f'<u:Created>{_fecha_sat(creado)}</u:Created>'
             f'<u:Expires>{_fecha_sat(expira)}</u:Expires>')
    a_firmar = ('<u:Timestamp xmlns:u="http://docs.oasis-open.org/wss/2004/01/'
                f'oasis-200401-wss-wssecurity-utility-1.0.xsd" u:Id="_0">{marca}</u:Timestamp>')
    key_info = (f'<KeyInfo><o:SecurityTokenReference><o:Reference URI="#{token_id}" '
                f'ValueType="{X509V3}"/></o:SecurityTokenReference></KeyInfo>')
    firma = crear_firma(fiel, a_firmar, '#_0', key_info)
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:u="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        '<s:Header><o:Security xmlns:o="http://docs.oasis-open.org/wss/2004/01/'
        'oasis-200401-wss-wssecurity-secext-1.0.xsd" s:mustUnderstand="1">'
        f'<u:Timestamp u:Id="_0">{marca}</u:Timestamp>'
        f'<o:BinarySecurityToken u:Id="{token_id}" ValueType="{X509V3}" '
        'EncodingType="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">{fiel["cert_b64"]}'
        f'</o:BinarySecurityToken>{firma}</o:Security></s:Header>'
        '<s:Body><Autentica xmlns="http://DescargaMasivaTerceros.gob.mx"/></s:Body></s:Envelope>')


def _sobre_firmado(fiel, operacion, nodo, atributos, extra=''):
    """Sobre de Solicitud / Verificacion / Descarga: la firma va dentro del
    nodo de la peticion y se calcula sobre la operacion con su namespace."""
    attrs = ' '.join(f'{k}="{_xml(v)}"' for k, v in sorted(atributos.items()) if v not in (None, ''))
    a_firmar = (f'<des:{operacion} xmlns:des="{NS_DES}"><des:{nodo} {attrs}>{extra}'
                f'</des:{nodo}></des:{operacion}>')
    firma = crear_firma(fiel, a_firmar)
    return (f'<s:Envelope {NS_SOBRE}><s:Header/><s:Body><des:{operacion}>'
            f'<des:{nodo} {attrs}>{extra}{firma}</des:{nodo}></des:{operacion}></s:Body></s:Envelope>')


def sobre_solicitud_emitidos(fiel, fecha_inicial, fecha_final, tipo_solicitud='CFDI',
                             tipo_comprobante=None, estado_comprobante=None):
    """fecha_* como texto AAAA-MM-DDTHH:MM:SS. El SAT exige inicial < final."""
    return _sobre_firmado(fiel, 'SolicitaDescargaEmitidos', 'solicitud', {
        'RfcSolicitante': fiel['rfc'], 'RfcEmisor': fiel['rfc'],
        'TipoSolicitud': tipo_solicitud, 'FechaInicial': fecha_inicial, 'FechaFinal': fecha_final,
        'TipoComprobante': tipo_comprobante, 'EstadoComprobante': estado_comprobante,
    })


def sobre_solicitud_folio(fiel, uuid):
    return _sobre_firmado(fiel, 'SolicitaDescargaFolio', 'solicitud',
                          {'RfcSolicitante': fiel['rfc'], 'Folio': uuid.lower()})


def sobre_verificacion(fiel, id_solicitud):
    return _sobre_firmado(fiel, 'VerificaSolicitudDescarga', 'solicitud',
                          {'IdSolicitud': id_solicitud, 'RfcSolicitante': fiel['rfc']})


def sobre_descarga(fiel, id_paquete):
    return _sobre_firmado(fiel, 'PeticionDescargaMasivaTercerosEntrada', 'peticionDescarga',
                          {'IdPaquete': id_paquete, 'RfcSolicitante': fiel['rfc']})


# =============================================================================
# LLAMADAS
# =============================================================================

def _post(url, accion, cuerpo, token=None):
    headers = {'Content-Type': 'text/xml; charset=utf-8', 'SOAPAction': accion}
    if token:
        headers['Authorization'] = f'WRAP access_token="{token}"'
    req = urllib.request.Request(url, data=cuerpo.encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detalle = e.read()[:1500].decode('utf-8', 'replace')
        falla = re.search(r'<faultstring[^>]*>(.*?)</faultstring>', detalle, re.S)
        raise ErrorSAT(f"HTTP {e.code} en {accion.rsplit('/', 1)[-1]}: "
                       f"{falla.group(1) if falla else detalle[:500]}")


def _nodo(raiz, nombre):
    for el in raiz.iter():
        if el.tag.rsplit('}', 1)[-1].lower() == nombre.lower():
            return el
    return None


def _atributos(el):
    return {k.rsplit('}', 1)[-1]: v for k, v in (el.attrib if el is not None else {}).items()}


def autenticar(fiel):
    resp = ET.fromstring(_post(URL_AUTENTICACION, ACCION_AUTENTICA, sobre_autenticacion(fiel)))
    nodo = _nodo(resp, 'AutenticaResult')
    if nodo is None or not (nodo.text or '').strip():
        raise ErrorSAT('La autenticacion no devolvio token.')
    return nodo.text.strip()


def _solicitar(token, operacion, sobre):
    resp = ET.fromstring(_post(URL_SOLICITUD, ACCION_SOLICITUD + operacion, sobre, token))
    a = _atributos(_nodo(resp, f'{operacion}Result'))
    return {'id_solicitud': a.get('IdSolicitud') or None, 'cod_estatus': a.get('CodEstatus'),
            'mensaje': a.get('Mensaje')}


def solicitar_emitidos(fiel, token, fecha_inicial, fecha_final, tipo_comprobante=None,
                       estado_comprobante='Vigente'):
    return _solicitar(token, 'SolicitaDescargaEmitidos',
                      sobre_solicitud_emitidos(fiel, fecha_inicial, fecha_final, 'CFDI',
                                               tipo_comprobante, estado_comprobante))


def solicitar_folio(fiel, token, uuid):
    return _solicitar(token, 'SolicitaDescargaFolio', sobre_solicitud_folio(fiel, uuid))


def verificar(fiel, token, id_solicitud):
    resp = ET.fromstring(_post(URL_VERIFICACION, ACCION_VERIFICA,
                               sobre_verificacion(fiel, id_solicitud), token))
    res = _nodo(resp, 'VerificaSolicitudDescargaResult')
    a = _atributos(res)
    estado = int(a.get('EstadoSolicitud') or 0)
    return {
        'estado': estado, 'estado_txt': ESTADO_SOLICITUD.get(estado, str(estado)),
        'cod_estatus': a.get('CodEstatus'), 'codigo_estado': a.get('CodigoEstadoSolicitud'),
        'numero_cfdis': int(a.get('NumeroCFDIs') or 0), 'mensaje': a.get('Mensaje'),
        'paquetes': [p.text.strip() for p in (res.iter() if res is not None else [])
                     if p.tag.rsplit('}', 1)[-1] == 'IdsPaquetes' and p.text],
    }


def descargar(fiel, token, id_paquete):
    """Devuelve (cod_estatus, mensaje, bytes del ZIP o None)."""
    resp = ET.fromstring(_post(URL_DESCARGA, ACCION_DESCARGA, sobre_descarga(fiel, id_paquete), token))
    a = _atributos(_nodo(resp, 'respuesta'))
    paquete = _nodo(resp, 'Paquete')
    datos = base64.b64decode(paquete.text) if paquete is not None and paquete.text else None
    return a.get('CodEstatus'), a.get('Mensaje'), datos


class SesionSAT:
    """Envuelve la e.firma y el token (recurso, como OdooModelProxy). El token
    dura ~5 min: se renueva solo a los 4."""

    def __init__(self, fiel):
        self.fiel = fiel
        self._token, self._desde = None, None

    def token(self):
        if not self._token or datetime.now(timezone.utc) - self._desde > timedelta(minutes=4):
            self._token, self._desde = autenticar(self.fiel), datetime.now(timezone.utc)
        return self._token
