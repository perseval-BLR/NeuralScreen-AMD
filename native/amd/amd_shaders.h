#pragma once

// Compute shaders for the AMD neural pass.
//
// The NVIDIA worker pipeline is RGBA8 end to end (the capture, the network's
// input and output, the present). The AMD engine is different in two ways:
//
//   1. It works on RGBA16F at the network extent, read and written in place.
//   2. Its residual machinery decodes sRGB around the edit, which is only the
//      identity if what it is given is linear to begin with. The desktop is
//      sRGB-encoded, so a decode on the way in and an encode on the way out
//      are what make the engine's own conversion transparent (the same
//      reasoning Magpie reached for its SrgbInput key, default on).
//
// Three small passes bridge that: in (RGBA8 -> RGBA16F, area-down or
// bilinear-up, optional sRGB decode), motion (R16G16F resize to the network
// extent, vectors preserved), out (RGBA16F -> RGBA8, bilinear-up, highlight
// shoulder, optional sRGB encode).
//
// The area/bilinear shapes mirror kScaleHlsl4 in quality_shaders.h - the
// filter is a reduction, not a resample: averaging is what a downscale needs
// and bilinear is what an enlargement needs.

static const char kAmdInHlsl[] = R"hlsl(
Texture2D<float4>   gSrc : register(t0);
RWTexture2D<float4> gDst : register(u0);
cbuffer P : register(b0) { uint gDstW; uint gDstH; uint gSrcW; uint gSrcH; };
#if SRGB
float3 ToLinear(float3 c) {
    return float3(
        c.r <= 0.04045 ? c.r / 12.92 : pow((c.r + 0.055) / 1.055, 2.4),
        c.g <= 0.04045 ? c.g / 12.92 : pow((c.g + 0.055) / 1.055, 2.4),
        c.b <= 0.04045 ? c.b / 12.92 : pow((c.b + 0.055) / 1.055, 2.4));
}
#endif
[numthreads(8,8,1)]
void CSMain(uint3 id : SV_DispatchThreadID) {
    if (id.x >= gDstW || id.y >= gDstH) return;
    int2 hi = int2(gSrcW-1, gSrcH-1);
    float4 c;
    if (gSrcW == gDstW && gSrcH == gDstH) {
        c = gSrc[id.xy];
    } else if (gSrcW > gDstW || gSrcH > gDstH) {
        float2 lo = float2(id.xy)*float2(gSrcW,gSrcH)/float2(gDstW,gDstH);
        float2 end = float2(id.xy+1)*float2(gSrcW,gSrcH)/float2(gDstW,gDstH);
        float4 sum = 0;
        [loop] for (int y=int(floor(lo.y)); y<int(ceil(end.y)); ++y) {
            float wy=max(0,min(end.y,float(y+1))-max(lo.y,float(y)));
            [loop] for (int x=int(floor(lo.x)); x<int(ceil(end.x)); ++x) {
                float wx=max(0,min(end.x,float(x+1))-max(lo.x,float(x)));
                sum += gSrc[clamp(int2(x,y),int2(0,0),hi)]*wx*wy;
            }
        }
        c = sum/((end.x-lo.x)*(end.y-lo.y));
    } else {
        float2 p=(float2(id.xy)+.5)*float2(gSrcW,gSrcH)/float2(gDstW,gDstH)-.5;
        int2 a=int2(floor(p)); float2 f=frac(p);
        float4 tl=gSrc[clamp(a,int2(0,0),hi)];
        float4 tr=gSrc[clamp(a+int2(1,0),int2(0,0),hi)];
        float4 bl=gSrc[clamp(a+int2(0,1),int2(0,0),hi)];
        float4 br=gSrc[clamp(a+int2(1,1),int2(0,0),hi)];
        c=lerp(lerp(tl,tr,f.x),lerp(bl,br,f.x),f.y);
    }
#if SRGB
    c.rgb = ToLinear(c.rgb);
#endif
    gDst[id.xy]=c;
}
)hlsl";

// The network's output leaves as linear RGBA16F at the network extent and has
// to become the RGBA8 frame the rest of the chain expects.
//
// The shoulder is Magpie's finding, kept because it is the difference between
// a usable and a clipped picture: the engine's tone curve pushes highlights
// well past 1.0 in linear, and an eight-bit target can only carry 1.0.
// Everything above the anchor is rolled off C1-continuously toward 1.0
// instead of being clipped flat - the same shape as the reference's, anchored
// at 0.85 by default (linear 1.0 lands near 0.925).
static const char kAmdOutHlsl[] = R"hlsl(
Texture2D<float4>   gSrc : register(t0);
RWTexture2D<float4> gDst : register(u0);
SamplerState gSamp : register(s0);
cbuffer P : register(b0) { uint gDstW; uint gDstH; float gShoulder; float gUnused; };
#if SRGB
float3 ToSrgb(float3 c) {
    return float3(
        c.r <= 0.0031308 ? c.r * 12.92 : 1.055 * pow(c.r, 1.0/2.4) - 0.055,
        c.g <= 0.0031308 ? c.g * 12.92 : 1.055 * pow(c.g, 1.0/2.4) - 0.055,
        c.b <= 0.0031308 ? c.b * 12.92 : 1.055 * pow(c.b, 1.0/2.4) - 0.055);
}
#endif
float Sh(float x, float a) {
    if (x <= a) return x;
    return a + (1.0 - a) * (1.0 - exp(-(x - a) / (1.0 - a)));
}
[numthreads(8,8,1)]
void CSMain(uint3 id : SV_DispatchThreadID) {
    if(id.x>=gDstW || id.y>=gDstH) return;
    float2 uv=(float2(id.xy)+.5)/float2(gDstW,gDstH);
    float3 c=gSrc.SampleLevel(gSamp, uv, 0).rgb;
    float a=clamp(gShoulder, 0.05, 0.99);
    c=float3(Sh(c.r,a),Sh(c.g,a),Sh(c.b,a));
#if SRGB
    c=ToSrgb(max(c, 0.0));
#endif
    gDst[id.xy]=float4(c,1.0);
}
)hlsl";

// The motion guide, resized to the network extent. Vectors are never scaled
// here - the packet's scaleX/scaleY carry the ratio, which is the
// reference's own convention for this hand-off. Area reduction for a
// downscale (average vectors, keep the direction), bilinear for an
// enlargement - the same filter split as the colour pass.
static const char kAmdMotionHlsl[] = R"hlsl(
Texture2D<float2>   gSrc : register(t0);
RWTexture2D<float2> gDst : register(u0);
cbuffer P : register(b0) { uint gDstW; uint gDstH; uint gSrcW; uint gSrcH; };
[numthreads(8,8,1)]
void CSMain(uint3 id : SV_DispatchThreadID) {
    if (id.x >= gDstW || id.y >= gDstH) return;
    int2 hi = int2(gSrcW-1, gSrcH-1);
    float2 v;
    if (gSrcW == gDstW && gSrcH == gDstH) {
        v = gSrc[id.xy];
    } else if (gSrcW > gDstW || gSrcH > gDstH) {
        float2 lo = float2(id.xy)*float2(gSrcW,gSrcH)/float2(gDstW,gDstH);
        float2 end = float2(id.xy+1)*float2(gSrcW,gSrcH)/float2(gDstW,gDstH);
        float2 sum = 0;
        [loop] for (int y=int(floor(lo.y)); y<int(ceil(end.y)); ++y) {
            float wy=max(0,min(end.y,float(y+1))-max(lo.y,float(y)));
            [loop] for (int x=int(floor(lo.x)); x<int(ceil(end.x)); ++x) {
                float wx=max(0,min(end.x,float(x+1))-max(lo.x,float(x)));
                sum += gSrc[clamp(int2(x,y),int2(0,0),hi)]*wx*wy;
            }
        }
        v = sum/((end.x-lo.x)*(end.y-lo.y));
    } else {
        float2 p=(float2(id.xy)+.5)*float2(gSrcW,gSrcH)/float2(gDstW,gDstH)-.5;
        int2 a=int2(floor(p)); float2 f=frac(p);
        float2 tl=gSrc[clamp(a,int2(0,0),hi)];
        float2 tr=gSrc[clamp(a+int2(1,0),int2(0,0),hi)];
        float2 bl=gSrc[clamp(a+int2(0,1),int2(0,0),hi)];
        float2 br=gSrc[clamp(a+int2(1,1),int2(0,0),hi)];
        v=lerp(lerp(tl,tr,f.x),lerp(bl,br,f.x),f.y);
    }
    gDst[id.xy]=v;
}
)hlsl";
