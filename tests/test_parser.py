import parser


def test_normalize_city_met_en_minuscule():
    assert parser.normalize_city("Bordeaux") == "bordeaux"


def test_normalize_city_retire_les_accents():
    assert parser.normalize_city("Saint-Étienne") == "saint-etienne"


def test_normalize_city_enleve_arrondissements():
    assert parser.normalize_city("PARIS 15E") == "paris"


def test_normalize_city_enleve_cedex():
    assert parser.normalize_city("Talence Cedex") == "talence"
