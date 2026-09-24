"""Judikatura: sběr rozhodnutí NS, NSS, ÚS a SDEU, AI shrnutí a zařazení.

Každý soud má adaptér v `judikatura/soudy/` (objeví nová rozhodnutí a dodá
jejich text), zbytek je společný: archiv v `data/judikatura/` (model, sklad),
AI rozbor (analyza), fronta s rozpočtem běhu (fronta) a běh jako celek
(orchestr). Vstupní bod je `scraper_judikatura.py`.
"""
