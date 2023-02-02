import unittest
from attgeo import AttGeo

class Test_ConfigHelper(unittest.TestCase):

    def test_run(self):
        url = "https://sampleserver6.arcgisonline.com/arcgis/rest/services/USA/MapServer/0"

        class Point:
            x: float
            y: float

        class City:
            objectid: int
            areaname: str
            pop2000: int
            shape: Point

        mapper = AttGeo(url, City)

        for city in mapper.read("areaname LIKE 'Arc%'", outSR=102100):
            print(city.__dict__)


if __name__ == '__main__':
    unittest.main()
