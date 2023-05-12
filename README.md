# Hagis

A GIS client

```python
from hagis import Layer, Point

class City:
    objectid: int
    areaname: str
    pop2000: int
    shape: Point

layer = Layer("https://sampleserver6.arcgisonline.com/arcgis/rest/services/USA/MapServer/0", City)

for city in layer.query():
    print(city.areaname, city.pop2000, city.shape.x, city.shape.y)
```

Provides [haggis typing](https://en.wikipedia.org/wiki/Wild_haggis), which is an advanced form of duck typing (except it's not clear if the animal in question actually exists).

[![Haggis](https://upload.wikimedia.org/wikipedia/commons/thumb/0/0b/Haggis_scoticus.jpg/300px-Haggis_scoticus.jpg)](https://en.wikipedia.org/wiki/Wild_haggis)

[More examples](https://github.com/jshirota/Hagis/blob/main/demo.ipynb)
