import os
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

pdfmetrics.registerFont(TTFont('Arial', 'C:\\Windows\\Fonts\\arial.ttf'))

downloads_dir = os.path.join(os.path.expanduser('~'), 'Downloads')
file1 = os.path.join(downloads_dir, '1_Old_Version_15.01.2024.pdf')
file2 = os.path.join(downloads_dir, '2_New_Version_20.08.2024.pdf')

c = canvas.Canvas(file1)
c.setFont('Arial', 14)
c.drawString(100, 700, "Указание Банка России № 123-У от 15.01.2024")
c.save()

c2 = canvas.Canvas(file2)
c2.setFont('Arial', 14)
c2.drawString(100, 700, "Указание Банка России № 123-У от 20.08.2024")
c2.save()

print(f"Created {file1} and {file2}")
