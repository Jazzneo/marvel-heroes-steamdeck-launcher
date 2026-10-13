"""Pure-Python LZO1X (the compression Marvel Heroes uses inside .upk packages): decompress, compress, compress_literal."""
def decompress(src, out_len):
    ip=0; op=bytearray(); n=len(src)
    def copy_match(dist, length):
        start=len(op)-dist
        if dist>=length: op.extend(op[start:start+length])
        else:
            for k in range(length): op.append(op[start+k])
    t=src[ip]
    state=0
    if t>17:
        t-=17; ip+=1
        op+=src[ip:ip+t]; ip+=t
        state = 1 if t<4 else 2   # after literal run
        t = src[ip]; ip+=1
    else:
        t = src[ip]; ip+=1
    while True:
        # t holds current instruction byte, state: 0=start of block (after match with 0 trailing lits), 1=after 1..3 literal(short), 2=after >=4 literals
        if t<16:
            if state==0:
                # literal run
                if t==0:
                    while src[ip]==0: t+=255; ip+=1
                    t+=15+src[ip]; ip+=1
                t+=3
                op+=src[ip:ip+t]; ip+=t
                state=2
                t=src[ip]; ip+=1
                continue
            elif state==2:
                # 3-byte match from distance 2049+
                dist=1+0x0800+(t>>2)+(src[ip]<<2); ip+=1
                copy_match(dist,3)
            else:
                # state 1: 2-byte match
                dist=1+(t>>2)+(src[ip]<<2); ip+=1
                copy_match(dist,2)
        else:
            if t>=64:
                dist=1+((t>>2)&7)+(src[ip]<<3); ip+=1
                length=(t>>5)-1+2
                copy_match(dist,length)
            elif t>=32:
                length=t&31
                if length==0:
                    while src[ip]==0: length+=255; ip+=1
                    length+=31+src[ip]; ip+=1
                length+=2
                ds=src[ip]|(src[ip+1]<<8); ip+=2
                dist=1+(ds>>2)
                copy_match(dist,length); t=ds&0xff
            else:  # 16..31
                dist=(t&8)<<11
                length=t&7
                if length==0:
                    while src[ip]==0: length+=255; ip+=1
                    length+=7+src[ip]; ip+=1
                length+=2
                ds=src[ip]|(src[ip+1]<<8); ip+=2
                dist+=ds>>2
                if dist==0:   # end of stream
                    break
                dist+=0x4000
                copy_match(dist,length); t=ds&0xff
        # trailing literals: low 2 bits of last byte
        tl=t&3
        if tl==0:
            state=0
        else:
            op+=src[ip:ip+tl]; ip+=tl
            state=1
        t=src[ip]; ip+=1
    assert len(op)==out_len,(len(op),out_len)
    return bytes(op)

def compress_literal(data):
    """Valid LZO1X stream that stores data as literals (no actual compression)."""
    n=len(data); out=bytearray()
    if n==0: return bytes([0x11,0,0])
    if n<=238:
        out.append(17+n)
    else:
        out.append(0); r=n-18
        if r<0:  # 239..? never (n>238)
            raise ValueError
        k,last=divmod(r,255)
        if last==0: k-=1; last=255
        out+=b'\0'*k; out.append(last)
    out+=data
    out+=bytes([0x11,0,0])
    return bytes(out)

def compress(data):
    """Real LZO1X compressor (greedy, M3/M4 matches only). Output decodes with lzo1x_decompress."""
    n=len(data); out=bytearray(); ip=0; lit_start=0
    table={}
    MAXD3=0x4000; MAXD4=0xBFFF
    def emit_literals(start, end, first):
        L=end-start
        if L==0: return
        if first:
            if L<=238:
                out.append(17+L); out.extend(data[start:end]); return
        # general literal run (L>=4), only valid when previous low bits were 0
        if L<=18: out.append(L-3)
        else:
            out.append(0); r=L-18; k,last=divmod(r,255)
            if last==0: k-=1; last=255
            out.extend(b'\0'*k); out.append(last)
        out.extend(data[start:end])
    def emit_match(dist, length, trailing):
        # trailing = number of literals (0..3) that follow, encoded in ds low bits
        if dist<=MAXD3:
            m=length-2
            if m<=31: out.append(32|m)
            else:
                out.append(32); r=m-31; k,last=divmod(r,255)
                if last==0: k-=1; last=255
                out.extend(b'\0'*k); out.append(last)
            ds=((dist-1)<<2)|trailing
        else:
            d2=dist-0x4000; hi=(d2>>14)&1; lo=d2&0x3FFF
            m=length-2
            if m<=7: out.append(16|(hi<<3)|m)
            else:
                out.append(16|(hi<<3)); r=m-7; k,last=divmod(r,255)
                if last==0: k-=1; last=255
                out.extend(b'\0'*k); out.append(last)
            ds=(lo<<2)|trailing
        out.append(ds&0xff); out.append(ds>>8)
    # gather matches first, then serialize with literal-count lookahead
    seq=[]  # (lit_start, lit_end, dist, length)
    i=0
    while i+4<=n:
        key=data[i:i+4]; cand=table.get(key); table[key]=i
        if cand is not None and 0<i-cand<=MAXD4 and not (i-cand==0x4000*0):
            dist=i-cand
            if dist>MAXD3 and (dist-0x4000)==0: cand=None
        if cand is not None and 0<i-cand<=MAXD4:
            L=4; mx=n-i
            while L<mx and L<2000 and data[cand+L]==data[i+L]: L+=1
            seq.append((lit_start,i,i-cand,L))
            for j in range(i+1,min(i+L,n-3)):
                table[data[j:j+4]]=j
            i+=L; lit_start=i
        else:
            i+=1
    # serialize
    first=True; pend_start=0
    for k,(ls,le,dist,L) in enumerate(seq):
        nlit=le-ls
        if first:
            if nlit==0: raise ValueError('stream cannot start with match')
            emit_literals(ls,le,True); first=False
        else:
            if nlit>=4: emit_literals(ls,le,False)
            # 1..3 literals were already emitted as trailing of previous match
        # how many literals follow this match before the next match / end?
        nxt_ls = ls if False else (le+L)
        nxt_le = seq[k+1][1] if k+1<len(seq) else n
        follow = nxt_le-nxt_ls
        trailing = follow if 1<=follow<=3 else 0
        emit_match(dist,L,trailing)
        if trailing: out.extend(data[nxt_ls:nxt_ls+trailing])
    # final literals
    tail_start = (seq[-1][1]+seq[-1][3]) if seq else 0
    tail = n-tail_start
    if not seq:
        emit_literals(0,n,True)
    elif tail>=4:
        emit_literals(tail_start,n,False)
    out.extend(b'\x11\x00\x00')
    return bytes(out)
