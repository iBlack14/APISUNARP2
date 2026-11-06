import unittest

from worker import parse_vehicle_ocr_texts


class VehicleOcrParsingTests(unittest.TestCase):
    def test_joins_split_values_and_ignores_watermarks_in_stable_order(self):
        fields = parse_vehicle_ocr_texts(
            [
                "NPLACA:",
                "0",
                "0874YA",
                "N·SERIE:",
                "SERIE-123",
                "NVIN:",
                "VIN-123",
                "NMOTOR:",
                "MOTOR-123",
                "COLOR:",
                "Sunarp",
                "ROJO",
                "MARCA:",
                "Esta",
                "BAJAJ",
                "MODELO:",
                "Sur",
                "REAUTORIKSHATORITO2TR",
                "PLACAVIGENTE:",
                "0874YA",
                "ANODEMODELO:2020",
                "Categoria: M1",
                "Año de Fab: 2019",
                "Potencia:",
                "110 HP",
                "Combustible: GASOLINA",
                "Cilindrada: 1984 CC",
                "Peso Bruto: 1700 KG",
                "Peso Neto: 1200 KG",
                "Carga Útil: 500 KG",
                "Asientos: 5",
                "Pasajeros: 4",
                "Fórmula Rodante: 4X2",
                "Número de Ejes: 2",
                "Número de Ruedas: 4",
                "Longitud: 4.5 M",
                "Ancho: 1.8 M",
                "Altura: 1.6 M",
                "PROPIETARIO(S):",
                "TEST OWNER",
                "06/11/2026 14:50:02",
            ]
        )

        self.assertEqual(fields["numero_placa"], "0874YA")
        self.assertEqual(fields["color"], "ROJO")
        self.assertEqual(fields["marca"], "BAJAJ")
        self.assertEqual(fields["modelo"], "REAUTORIKSHATORITO2TR")
        self.assertEqual(fields["anio_modelo"], "2020")
        self.assertEqual(fields["categoria"], "M1")
        self.assertEqual(fields["anio_fabricacion"], "2019")
        self.assertEqual(fields["potencia"], "110 HP")
        self.assertEqual(fields["combustible"], "GASOLINA")
        self.assertEqual(fields["cilindrada"], "1984 CC")
        self.assertEqual(fields["peso_bruto"], "1700 KG")
        self.assertEqual(fields["peso_neto"], "1200 KG")
        self.assertEqual(fields["carga_util"], "500 KG")
        self.assertEqual(fields["numero_asientos"], "5")
        self.assertEqual(fields["numero_pasajeros"], "4")
        self.assertEqual(fields["formula_rodante"], "4X2")
        self.assertEqual(fields["numero_ejes"], "2")
        self.assertEqual(fields["numero_ruedas"], "4")
        self.assertEqual(fields["longitud"], "4.5 M")
        self.assertEqual(fields["ancho"], "1.8 M")
        self.assertEqual(fields["altura"], "1.6 M")
        self.assertEqual(fields["propietarios"], ["TEST OWNER"])
        self.assertEqual(
            list(fields),
            [
                "numero_placa",
                "numero_serie",
                "numero_vin",
                "numero_motor",
                "modelo",
                "marca",
                "color",
                "placa_vigente",
                "anio_modelo",
                "anio_fabricacion",
                "categoria",
                "potencia",
                "cilindrada",
                "combustible",
                "formula_rodante",
                "peso_bruto",
                "peso_neto",
                "carga_util",
                "longitud",
                "ancho",
                "altura",
                "numero_ejes",
                "numero_ruedas",
                "numero_asientos",
                "numero_pasajeros",
                "propietarios",
            ],
        )


if __name__ == "__main__":
    unittest.main()
