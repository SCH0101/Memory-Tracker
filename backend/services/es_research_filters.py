"""Memory policy mapped onto the public ES filter contract; no ES imports."""
from datetime import date
from .es_rag_gateway import EsRagContractError

SOURCE_TYPES = {
    'broker_research':['initiation','earnings_preview','earnings_review','company_update','primer','conference_notes'],
    'sellside':['initiation','earnings_preview','earnings_review','company_update','primer','conference_notes'],
    'expert_call':['expert_call'], 'company_presentation':['company_presentation'],
    'industry_report':['industry_report'], 'earnings_transcript':['earnings_call_transcript','transcript'],
    'transcript':['earnings_call_transcript','transcript'],
}
FIELDS={'themes','tickers','sectors','industries','brokers','document_types',
        'file_formats','source_types','date_from','date_to'}
ALIASES={'theme':'themes','ticker':'tickers','sector':'sectors','broker':'brokers',
         'primary_niche_sector':'sectors','industry':'industries',
         'date_after':'date_from','date_before':'date_to'}


def memory_filters(filters=None, metadata_filters=None):
    result={'themes':['ai']}
    def merge(key,value):
        if key=='themes':
            if value!=['ai']:
                raise EsRagContractError('Memory Tracker requires the AI industry theme',operation='filters')
        elif key in result and result[key]!=value:
            raise EsRagContractError('Conflicting filter dimensions; supply each once',operation='filters')
        result[key]=value
    def consume(obj):
        for key,value in (obj or {}).items():
            if key=='$and':
                for child in value: consume(child)
                continue
            if key=='year':
                try:
                    year=int(value)
                    merge('date_from',date(year,1,1).isoformat()); merge('date_to',date(year,12,31).isoformat())
                except (ValueError,TypeError):
                    raise EsRagContractError('Invalid year filter',operation='filters') from None
                continue
            if key in {'doc_type','source_doc_type'}:
                if value not in SOURCE_TYPES:
                    raise EsRagContractError('Unsupported source-type filter',operation='filters')
                merge('document_types',SOURCE_TYPES[value]); continue
            key=ALIASES.get(key,key)
            if key not in FIELDS:
                raise EsRagContractError('Unsupported research filter; use the documented ES fields',operation='filters')
            if isinstance(value,dict):
                if set(value)=={'$eq'}: value=value['$eq']
                elif set(value)=={'$in'}: value=value['$in']
                else: raise EsRagContractError('Unsupported filter operator',operation='filters')
            if key not in {'date_from','date_to'} and isinstance(value,str): value=[value]
            if key=='tickers': value=[' '.join(v.split()).upper() for v in value]
            merge(key,value)
    consume(filters); consume(metadata_filters)
    return result
