# Hagis

A GIS client

```python
from hagis import Layer, Point

class City:
    objectid: int
    areaname: str
    pop2000: int
    geometry: Point

layer = Layer("https://sampleserver6.arcgisonline.com/arcgis/rest/services/USA/MapServer/0", City)

for city in layer.query():
    print(city.areaname, city.pop2000, city.geometry.x, city.geometry.y)
```

[More examples](https://github.com/jshirota/Hagis/blob/main/demo.ipynb)
