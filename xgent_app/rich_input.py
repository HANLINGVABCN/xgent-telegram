"""Official Rich block fields are data; buttons and unknown fields are not text."""
from __future__ import annotations

TEXT_TYPES={'bold','italic','underline','strikethrough','spoiler','marked','subscript','superscript','code',
 'date_time','text_mention','custom_emoji','mathematical_expression','url','email_address','phone_number',
 'bank_card_number','mention','hashtag','cashtag','bot_command','anchor','anchor_link','reference','reference_link',
 'link','text_link','pre'}
BLOCK_TYPES={'paragraph','section_heading','heading','preformatted','pre','footer','list','table','blockquote',
 'block_quotation','expandable_block_quotation','pull_quotation','details','collage','slideshow','anchor',
 'mathematical_expression','photo','video','document','audio','voice_note','animation','map'}
MEDIA_TYPES={'photo','video','document','audio','voice_note','animation'}


def text(value,markdown=False):
    if isinstance(value,str):return value
    if isinstance(value,list):return ''.join(text(item,markdown) for item in value)
    if not isinstance(value,dict) or value.get('type','') not in TEXT_TYPES|{''}:return ''
    kind=value.get('type','');body=text(value.get('text',''),markdown)
    if not markdown:return body
    tags={'bold':'**','italic':'*','strikethrough':'~~','underline':'__','spoiler':'||','code':'`','pre':'```'}
    if kind in tags:return tags[kind]+body+tags[kind]
    if kind in {'url','link','text_link'} and value.get('url'):return '['+body+']('+value['url']+')'
    if kind=='mathematical_expression':return '$'+body+'$'
    return body


def block(value,markdown=False):
    if not isinstance(value,dict) or value.get('type') not in BLOCK_TYPES:return ''
    kind=value['type']
    if kind=='table':
        rows=[]
        for row in value.get('cells',[]):
            if isinstance(row,list):rows.append([text(cell.get('text',''),markdown) for cell in row if isinstance(cell,dict)])
        result=[]
        for index,row in enumerate(rows):
            result.append('| '+' | '.join(row)+' |' if markdown else ' | '.join(row))
            if markdown and index==0:result.append('| '+' | '.join(['---']*len(row))+' |')
        return '\n'.join(result)
    if kind=='list':
        return '\n'.join(str(item.get('label','-'))+' '+'\n'.join(block(child,markdown) for child in item.get('blocks',[]))
                         for item in value.get('items',[]) if isinstance(item,dict))
    if kind in MEDIA_TYPES:
        caption=value.get('caption') or {}
        return text(caption.get('text',''),markdown) if isinstance(caption,dict) else text(caption,markdown)
    if kind in {'preformatted','pre'}:
        body=text(value.get('text',''),False)
        return '```'+str(value.get('language') or '')+'\n'+body+'\n```' if markdown else body
    body=text(value.get('text',''),markdown)
    if isinstance(value.get('blocks'),list):body='\n'.join(block(child,markdown) for child in value['blocks'])
    if kind=='details':body=text(value.get('summary',''),markdown)+'\n'+body
    if markdown and kind in {'blockquote','block_quotation','expandable_block_quotation','pull_quotation'}:
        return '\n'.join('> '+line for line in body.splitlines())
    return body


def media(value):
    if not isinstance(value,dict):return []
    result=[]
    for part in value.get('blocks',[]):
        if not isinstance(part,dict) or part.get('type') not in BLOCK_TYPES:continue
        kind=part['type']
        if kind in MEDIA_TYPES and isinstance(part.get(kind),dict) and part[kind].get('file_id'):
            result.append((kind,part[kind]))
        if kind=='photo' and isinstance(part.get('photo'),list):
            result.append(('photo',part['photo']))
        for container in ('blocks','items'):
            children=part.get(container,[])
            if isinstance(children,list):
                for child in children:
                    result.extend(media({'blocks':[child]}) if container=='blocks' else media(child))
    return result


class Overlay:
    def __init__(self,original,**fields):self.original=original;self.fields=fields
    def __getattr__(self,name):return self.fields[name] if name in self.fields else getattr(self.original,name)


def media_update(update,kind,value,caption):
    from telegram import Document,PhotoSize,Video,Audio,Voice,Animation
    classes={'document':Document,'video':Video,'audio':Audio,'voice_note':Voice,'animation':Animation}
    fields={name:None for name in ('document','photo','video','audio','voice','animation','text')}
    if kind=='photo':fields['photo']=[PhotoSize.de_json(item,None) for item in value]
    else:fields['voice' if kind=='voice_note' else kind]=classes[kind].de_json(value,None)
    fields['caption']=caption;fields['api_kwargs']={}
    message=Overlay(update.message,**fields)
    return Overlay(update,message=message,effective_message=message)
