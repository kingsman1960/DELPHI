import yaml

print(yaml.load("""
 - Hesperiidae
 - Papilionidae
 - Apatelodidae
 - Epiplemidae
 """, Loader=yaml.Loader)
      )