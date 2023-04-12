# Hagis

A High Availability GIS client.

```python
from hagis import Mapper, Point

url = "https://sampleserver6.arcgisonline.com/arcgis/rest/services/USA/MapServer/0"

class City:
    objectid: int
    areaname: str
    pop2000: int
    shape: Point

mapper = Mapper(url, City)

for city in mapper.query():
    print(city.areaname, city.pop2000, city.shape.x, city.shape.y)
```

[More examples](https://github.com/jshirota/Hagis/blob/main/demo.ipynb)
