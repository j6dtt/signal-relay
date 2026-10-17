# Moves each <figure> to the end of its <section class="level2">: the text follows its heading, then each
# diagram gets its own landscape page (see style.css, figure { page: wide }). Used by render.sh.
import sys
from bs4 import BeautifulSoup
p = sys.argv[1]
soup = BeautifulSoup(open(p, encoding="utf-8").read(), "html.parser")
for sec in soup.select("section.level2"):
    figs = sec.find_all("figure")
    for f in figs:
        sec.append(f.extract())          # diagrams go after the section's text, each on its own landscape page
open(p, "w", encoding="utf-8").write(str(soup))
