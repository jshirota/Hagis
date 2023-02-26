import unittest
from attgeo import Mapper


class Test_Mapper(unittest.TestCase):

    def test_city(self):
        url = "https://sampleserver6.arcgisonline.com/arcgis/rest/services/USA/MapServer/0"

        class Point:
            x: float
            y: float

        class City:
            objectid: int
            areaname: str
            shape: Point

        mapper = Mapper(url, City,  mapping={"pop2000": "pop2000"})

        for city in mapper.query("areaname LIKE 'Arc%'", outSR=102100):
            self.assertTrue(city.areaname.startswith("Arc"))
            self.assertTrue(getattr(city, "pop2000") > 0)


if __name__ == '__main__':
    unittest.main()
