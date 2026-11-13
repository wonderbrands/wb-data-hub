import re
import requests
from lxml import etree

SAT_WSDL = "https://consultaqr.facturaelectronica.sat.gob.mx/ConsultaCFDIService.svc"

NS = {
    "cfdi": "http://www.sat.gob.mx/cfd/4",
    "tfd": "http://www.sat.gob.mx/TimbreFiscalDigital",
}


def extraer_datos_cfdi(ruta_xml):
    """Lee un XML de CFDI y extrae los datos que necesita consultar_sat()."""
    tree = etree.parse(ruta_xml)
    root = tree.getroot()

    emisor = root.find("cfdi:Emisor", NS)
    receptor = root.find("cfdi:Receptor", NS)
    timbre = root.find("cfdi:Complemento/tfd:TimbreFiscalDigital", NS)

    sello = root.get("Sello", "")

    return {
        "rfc_emisor": emisor.get("Rfc"),
        "rfc_receptor": receptor.get("Rfc"),
        "total": root.get("Total"),
        "uuid": timbre.get("UUID"),
        "sello_ultimos_8": sello[-8:],
    }


def consultar_sat(cfdi, timeout=30):
    """Consulta el ConsultaCFDIService del SAT (SOAP) y devuelve el estado.

    Este es el ÚNICO dato que resuelve la duda de fondo: si el CFDI figura
    como 'Cancelado' ante el SAT, la venta ya quedó anulada fiscalmente y
    NO debe existir ninguna nota de crédito."""
    expresion = (
        f"?re={cfdi['rfc_emisor']}"
        f"&rr={cfdi['rfc_receptor']}"
        f"&tt={cfdi['total']}"
        f"&id={cfdi['uuid']}"
        f"&fe={cfdi['sello_ultimos_8']}"
    )
    envelope = f"""<soapenv:Envelope
        xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
        xmlns:tem="http://tempuri.org/">
      <soapenv:Header/>
      <soapenv:Body>
        <tem:Consulta>
          <tem:expresionImpresa><![CDATA[{expresion}]]></tem:expresionImpresa>
        </tem:Consulta>
      </soapenv:Body>
    </soapenv:Envelope>"""

    resp = requests.post(
        SAT_WSDL,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://tempuri.org/IConsultaCFDIService/Consulta",
        },
        timeout=timeout,
    )

    def tag(nombre):
        m = re.search(rf"<a:{nombre}>(.*?)</a:{nombre}>", resp.text, re.S)
        return m.group(1).strip() if m else None

    return {
        "http_status": resp.status_code,
        "codigo_estatus": tag("CodigoEstatus"),
        "estado": tag("Estado"),                        # Vigente | Cancelado | No Encontrado
        "es_cancelable": tag("EsCancelable"),
        "estatus_cancelacion": tag("EstatusCancelacion"),
        "validacion_efos": tag("ValidacionEFOS"),
        "expresion_consultada": expresion,
    }


if __name__ == "__main__":
    ruta = r"C:\Users\Sergio Gil Guerrero\Documents\WonderBrands\Repos\wb-data-hub\test\financial\credit_note_refund_processes\53775514-2D7E-432B-BF59-3E0957B36B74.xml"  # ajusta la ruta si es necesario

    cfdi = extraer_datos_cfdi(ruta)
    print("Datos extraídos del XML:")
    for k, v in cfdi.items():
        print(f"  {k}: {v}")

    print("\nConsultando SAT...")
    resultado = consultar_sat(cfdi)

    print("\nResultado:")
    for k, v in resultado.items():
        print(f"  {k}: {v}")