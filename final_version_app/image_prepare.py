"""Prepare a bounded image/PDF page in a disposable process for visual tools."""
import json,sys
from pathlib import Path

def prepare(source,name,destination,page=1,crop=None,max_edge=2048,output_format="PNG"):
    if not 64<=max_edge<=2048 or output_format not in {"PNG","JPEG"}:raise ValueError("Invalid image output options")
    from PIL import Image
    Image.MAX_IMAGE_PIXELS=25_000_000
    if Path(name).suffix.lower()=='.pdf':
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(source) as pdf:
            if not 1<=page<=len(pdf):raise ValueError('PDF page is out of range')
            pdf_page=pdf[page-1]
            width,height=pdf_page.get_size();scale=min(2.0,2048/max(width,height))
            bitmap=pdf_page.render(scale=scale);image=bitmap.to_pil().copy();bitmap.close();pdf_page.close()
    else:
        with Image.open(source) as original:
            if original.width*original.height>25_000_000:raise ValueError('Image exceeds pixel limit')
            original.seek(0);image=original.convert('RGB')
    if crop:
        if len(crop)!=4 or not all(isinstance(x,int) for x in crop):raise ValueError('crop must be four integer pixel coordinates')
        left,top,right,bottom=crop
        if not (0<=left<right<=image.width and 0<=top<bottom<=image.height):raise ValueError('Crop is outside image boundaries')
        image=image.crop(tuple(crop))
    image.thumbnail((max_edge,max_edge));image.save(destination,format=output_format,quality=80);image.close()

if __name__=='__main__':
    if sys.platform!='win32':
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(25,25))
        resource.setrlimit(resource.RLIMIT_AS,(384*1024*1024,384*1024*1024))
    options=json.loads(sys.argv[1]);prepare(**options)
