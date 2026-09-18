"""Polygon measurements by exact piecewise-linear scanline integration (pixel coordinates)."""
import math


def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])

def intersection_y(a,b,c,d):
    dx,dy=b[0]-a[0],b[1]-a[1];ex,ey=d[0]-c[0],d[1]-c[1]
    den=dx*ey-dy*ex
    if abs(den)<1e-12:return None
    t=((c[0]-a[0])*ey-(c[1]-a[1])*ex)/den
    u=((c[0]-a[0])*dy-(c[1]-a[1])*dx)/den
    return a[1]+t*dy if 0<=t<=1 and 0<=u<=1 else None

def validate_polygon(points,dimensions):
    if not 3<=len(points)<=128:raise ValueError('Use 3–128 vertices per polygon')
    w,h=dimensions
    if any(not math.isfinite(v) for p in points for v in p):raise ValueError('Coordinates must be finite')
    if any(not 0<=x<=w or not 0<=y<=h for x,y in points):raise ValueError('Region extends outside the image')
    edges=list(zip(points,points[1:]+points[:1]))
    for i,(a,b) in enumerate(edges):
        if a==b:raise ValueError('Duplicate consecutive vertices')
        for j,(c,d) in enumerate(edges[i+1:],i+1):
            if j==i+1 or (i==0 and j==len(edges)-1):continue
            if intersection_y(a,b,c,d) is not None:raise ValueError('Polygon crosses itself; move the crossing vertices')
            if abs(cross(a,b,c))<1e-9 and abs(cross(a,b,d))<1e-9:
                if max(min(a[0],b[0]),min(c[0],d[0]))<=min(max(a[0],b[0]),max(c[0],d[0])) and max(min(a[1],b[1]),min(c[1],d[1]))<=min(max(a[1],b[1]),max(c[1],d[1])):raise ValueError('Polygon edges overlap')
    if abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in edges))/2<1e-6:raise ValueError('Region has no area')

def merged(intervals):
    out=[]
    for a,b in sorted(intervals):
        if out and a<=out[-1][1]:out[-1][1]=max(out[-1][1],b)
        else:out.append([a,b])
    return out

def intervals(polys,y):
    spans=[]
    for p in polys:
        xs=[]
        for a,b in zip(p,p[1:]+p[:1]):
            if min(a[1],b[1])<y<max(a[1],b[1]):xs.append(a[0]+(y-a[1])*(b[0]-a[0])/(b[1]-a[1]))
        xs.sort();spans.extend(zip(xs[::2],xs[1::2]))
    return merged(spans)

def difference(a,b):
    out=[]
    for l,r in a:
        for x,y in b:
            if y<=l or x>=r:continue
            if x>l:out.append([l,min(x,r)])
            l=max(l,y)
            if l>=r:break
        if l<r:out.append([l,r])
    return out

def intersect(a,b):return [[max(l,x),min(r,y)] for l,r in a for x,y in b if max(l,x)<min(r,y)]

def measurements(regions,mpp):
    categories={kind:[r['points'] for r in regions if r['kind']==kind] for kind in ['tumour','tissue','exclusion']}
    polys=sum(categories.values(),[])
    edges=[(a,b) for p in polys for a,b in zip(p,p[1:]+p[:1])]
    cuts={p[1] for poly in polys for p in poly}
    for i,(a,b) in enumerate(edges):
        for c,d in edges[i+1:]:
            if max(a[0],b[0])<min(c[0],d[0]) or max(c[0],d[0])<min(a[0],b[0]):continue
            y=intersection_y(a,b,c,d)
            if y is not None:cuts.add(y)
    areas=[0.,0.,0.];ys=sorted(cuts)
    for low,high in zip(ys,ys[1:]):
        for y in [low+(high-low)*.25,low+(high-low)*.75]:
            excluded=intervals(categories['exclusion'],y)
            tumour=difference(intervals(categories['tumour'],y),excluded)
            tissue=difference(intervals(categories['tissue'],y),excluded)
            within=intersect(tumour,tissue)
            for i,spans in enumerate([tumour,tissue,within]):areas[i]+=sum(b-a for a,b in spans)*(high-low)/2
    factor=mpp[0]*mpp[1]/1e6 if all(mpp) else None
    individual=[]
    for r in regions:
        p=r['points'];area=abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(p,p[1:]+p[:1])))/2
        individual.append({'id':r.get('id'),'area_px2':area,'area_mm2':area*factor if factor else None})
    return {'individual_areas':individual,'tumour_area_px2':areas[0],'tissue_area_px2':areas[1],'tumour_within_tissue_px2':areas[2],
            'tumour_area_mm2':areas[0]*factor if factor else None,'tissue_area_mm2':areas[1]*factor if factor else None,
            'tumour_area_percent':100*areas[2]/areas[1] if areas[1]>0 else None,
            'definition':'Union of tumour polygons minus exclusions; percentage uses tumour intersected with the union of tissue-boundary polygons minus exclusions. This is area percentage, not tumour-cell percentage.'}
