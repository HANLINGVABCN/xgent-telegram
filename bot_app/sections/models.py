# This file is executed by bot_server.py in the shared application namespace.
# Keep cross-section names available through the loader until the next decoupling phase.

class ModelClient:
    _VALID_ROLES = {'user', 'assistant', 'system'}
    
    @staticmethod
    def clean_memories(history_list: list) -> list:
        clean_history = []
        for msg in history_list:
            role = msg.get('role')
            if role == 'model': 
                role = 'assistant'
            if role not in ModelClient._VALID_ROLES:
                continue  # 跳过无效角色
            content = msg.get('content', "")
            normalized_content = ModelClient._normalize_content(content)
            if normalized_content:
                clean_history.append({"role": role, "content": normalized_content})
        return clean_history

    @staticmethod
    def _normalize_content(content: Any) -> Optional[Any]:
        if isinstance(content, list):
            normalized_parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue

                part_type = part.get('type')
                if part_type == 'text':
                    text = str(part.get('text', '')).strip()
                    if text:
                        normalized_parts.append({"type": "text", "text": text})
                elif part_type == 'image':
                    data = part.get('data')
                    if not data:
                        continue
                    normalized_parts.append({
                        "type": "image",
                        "mime_type": str(part.get('mime_type') or 'image/jpeg'),
                        "data": str(data)
                    })
                elif part_type == 'binary':
                    data = part.get('data')
                    if not data:
                        continue
                    normalized_parts.append({
                        "type": "binary",
                        "mime_type": str(part.get('mime_type') or 'application/octet-stream'),
                        "filename": str(part.get('filename') or 'binary_file'),
                        "data": str(data)
                    })

            return normalized_parts or None

        if content is None:
            return None

        text = str(content)
        return text if text else None

    @staticmethod
    def _to_openai_content(content: Any) -> Any:
        if isinstance(content, str):
            return content

        openai_parts = []
        for part in content or []:
            part_type = part.get('type')
            if part_type == 'text':
                openai_parts.append({"type": "text", "text": part['text']})
            elif part_type == 'image':
                mime_type = part.get('mime_type', 'image/jpeg')
                openai_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{part['data']}"
                    }
                })

        return openai_parts

    @staticmethod
    def _to_gemini_parts(content: Any) -> List[Dict[str, Any]]:
        if isinstance(content, str):
            return [{"text": content}]

        gemini_parts = []
        for part in content or []:
            part_type = part.get('type')
            if part_type == 'text':
                gemini_parts.append({"text": part['text']})
            elif part_type == 'image':
                gemini_parts.append({
                    "inline_data": {
                        "mime_type": part.get('mime_type', 'image/jpeg'),
                        "data": part['data']
                    }
                })
            elif part_type == 'binary':
                gemini_parts.append({
                    "inline_data": {
                        "mime_type": part.get('mime_type', 'application/octet-stream'),
                        "data": part['data']
                    }
                })

        return gemini_parts

    @staticmethod
    def _to_claude_content(content: Any) -> Any:
        if isinstance(content, str):
            return content

        claude_parts = []
        for part in content or []:
            part_type = part.get('type')
            if part_type == 'text':
                claude_parts.append({"type": "text", "text": part['text']})
            elif part_type == 'image':
                claude_parts.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.get('mime_type', 'image/jpeg'),
                        "data": part['data']
                    }
                })

        return claude_parts

    @staticmethod
    def _build_gemini_contents(history: list) -> List[Dict[str, Any]]:
        contents = []
        for msg in ModelClient.clean_memories(history):
            role = 'model' if msg['role'] == 'assistant' else 'user'
            parts = ModelClient._to_gemini_parts(msg['content'])
            if parts:
                contents.append({"role": role, "parts": parts})
        return contents

    @staticmethod
    def _build_claude_messages(history: list) -> List[Dict[str, Any]]:
        messages = []
        for msg in ModelClient.clean_memories(history):
            # Claude 消息仅支持 user/assistant；系统旁白（[系统操作] 前缀）降级为 user 保留内容
            role = 'user' if msg['role'] == 'system' else msg['role']
            messages.append({
                "role": role,
                "content": ModelClient._to_claude_content(msg['content'])
            })
        return messages

    @staticmethod
    def _media_part_to_data_url(part: Dict[str, Any]) -> Optional[str]:
        image_url = part.get('image_url')
        if isinstance(image_url, dict):
            url = str(image_url.get('url') or '')
            if url.lower().startswith(INLINE_MEDIA_PREFIXES):
                return url
        elif isinstance(image_url, str) and image_url.lower().startswith(INLINE_MEDIA_PREFIXES):
            return image_url

        inline_data = part.get('inline_data') or part.get('inlineData')
        if isinstance(inline_data, dict):
            data = inline_data.get('data')
            mime_type = inline_data.get('mime_type') or inline_data.get('mimeType')
            if data and mime_type:
                return f"data:{mime_type};base64,{data}"

        data = part.get('data') or part.get('base64') or part.get('b64_json')
        mime_type = (
            part.get('mime_type')
            or part.get('mimeType')
            or part.get('media_type')
            or part.get('mediaType')
        )
        if data and mime_type and str(mime_type).lower().startswith(('image/', 'video/', 'audio/')):
            return f"data:{mime_type};base64,{data}"

        url = part.get('url')
        if isinstance(url, str) and url.lower().startswith(INLINE_MEDIA_PREFIXES):
            return url

        return None

    @staticmethod
    def _model_content_to_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            pieces = [ModelClient._model_content_to_text(part) for part in content]
            return "\n".join(piece for piece in pieces if piece)
        if isinstance(content, dict):
            pieces: List[str] = []
            text = content.get('text') or content.get('output_text')
            if text:
                pieces.append(str(text))

            data_url = ModelClient._media_part_to_data_url(content)
            if data_url:
                pieces.append(data_url)

            for key in ('content', 'parts', 'images', 'image', 'output'):
                nested = content.get(key)
                if nested is not None:
                    nested_text = ModelClient._model_content_to_text(nested)
                    if nested_text:
                        pieces.append(nested_text)

            return "\n".join(piece for piece in pieces if piece)

        return str(content)

    @staticmethod
    def _extract_gemini_text_response(data: Dict[str, Any]) -> Optional[str]:
        candidates = data.get('candidates', [])
        if not candidates:
            return None

        text_parts = []
        for part in candidates[0].get('content', {}).get('parts', []):
            if not isinstance(part, dict):
                continue
            text = ModelClient._model_content_to_text(part)
            if text:
                text_parts.append(text)

        full_text = '\n'.join(text_parts).strip()
        return full_text or None

    @staticmethod
    def _extract_claude_text_response(data: Dict[str, Any]) -> Optional[str]:
        text_parts = []
        for block in data.get('content', []):
            if not isinstance(block, dict):
                continue
            if block.get('type') != 'text':
                continue
            text = block.get('text', '')
            if text:
                text_parts.append(text)

        full_text = ''.join(text_parts).strip()
        return full_text or None

    @staticmethod
    def _chat_completions_api(client: AsyncOpenAI) -> Any:
        """兼容 OpenAI SDK 的动态重载，避免编辑器误报 create() 参数不匹配。"""
        return cast(Any, client.chat.completions)

    @staticmethod
    def _build_stream_timeout(read_timeout: Optional[float] = None) -> Any:
        import httpx
        if read_timeout is None:
            configured_timeout = normalize_stream_timeout(UserDataManager.get('stream_timeout', 0))
            read_timeout = None if configured_timeout <= 0 else configured_timeout
        return httpx.Timeout(connect=20.0, read=read_timeout, write=60.0, pool=60.0)

    @staticmethod
    def _openai_compatible_headers(api_key: str, accept: str = "application/json") -> Dict[str, str]:
        return {
            **PROVIDER_HTTP_HEADERS,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": accept,
        }

    @staticmethod
    def _build_openai_messages(system_prompt: str, history: list) -> List[Dict[str, Any]]:
        messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
        for msg in ModelClient.clean_memories(history):
            messages.append({
                "role": msg['role'],
                "content": ModelClient._to_openai_content(msg['content'])
            })
        return messages

    @staticmethod
    def _extract_openai_compatible_text(data: Dict[str, Any]) -> str:
        texts: List[str] = []
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            delta = choice.get("delta") or {}
            for value in (message.get("content"), delta.get("content"), choice.get("text")):
                text = ModelClient._model_content_to_text(value)
                if text:
                    texts.append(text)
        return "".join(texts)

    @staticmethod
    def _extract_openai_compatible_sse_text(text: str, usage_sink: Optional[List[Dict[str, int]]] = None) -> str:
        texts: List[str] = []
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            record_token_usage(usage_sink, data.get("usage"))
            chunk_text = ModelClient._extract_openai_compatible_text(data)
            if chunk_text:
                texts.append(chunk_text)
        return "".join(texts)

    @staticmethod
    async def _fetch_openai_compatible_models(api_key: str, base_url: str) -> list:
        import httpx
        url = f"{base_url.rstrip('/')}/models"
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=ModelClient._openai_compatible_headers(api_key))
            if resp.status_code >= 400:
                logger.error(f"OpenAI compatible models list error: {resp.status_code}: {redact_sensitive_text(resp.text or '')[:500]}")
                return []
            data = resp.json()
            model_ids = []
            for m in data.get("data") or data.get("models") or []:
                if isinstance(m, dict):
                    model_id = m.get("id") or m.get("name")
                else:
                    model_id = str(m)
                if model_id and 'embedding' not in str(model_id).lower() and 'audio' not in str(model_id).lower():
                    model_id_str = str(model_id)
                    if model_id_str.startswith("models/"):
                        model_id_str = model_id_str[len("models/"):]
                    model_ids.append(model_id_str)
            return sorted(model_ids)
        except Exception as e:
            logger.error(f"OpenAI compatible fetch error: {format_provider_exception(e)}")
            return []

    @staticmethod
    async def _complete_openai_compatible_http(api_key: str, base_url: str, model: str,
                                              system_prompt: str, history: list,
                                              max_tokens: Optional[int] = None,
                                              usage_sink: Optional[List[Dict[str, int]]] = None,
                                              trace_id: Optional[str] = None,
                                              prov_name: str = "") -> Tuple[Optional[str], Optional[str]]:
        import httpx
        url = f"{base_url.rstrip('/')}/chat/completions"
        body: Dict[str, Any] = {
            "model": model,
            "messages": ModelClient._build_openai_messages(system_prompt, history),
            "temperature": 0.7,
            "stream": False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        write_model_trace("model_request", {
            "trace_id": trace_id,
            "provider": prov_name,
            "provider_format": "openai_compatible_http",
            "model": model,
            "stream": False,
            "request": body,
        })

        try:
            async with httpx.AsyncClient(timeout=ModelClient._build_stream_timeout(), follow_redirects=True) as client:
                resp = await client.post(
                    url,
                    json=body,
                    headers=ModelClient._openai_compatible_headers(api_key),
                )
            if resp.status_code >= 400:
                error_text = redact_sensitive_text(resp.text or '')
                write_model_trace("model_error", {
                    "trace_id": trace_id,
                    "provider": prov_name,
                    "provider_format": "openai_compatible_http",
                    "model": model,
                    "stream": False,
                    "status_code": resp.status_code,
                    "error": error_text,
                })
                return None, f"OpenAI compatible API error ({resp.status_code}): {error_text}"

            raw_text = resp.text or ""
            if raw_text.lstrip().startswith("data:"):
                text = ModelClient._extract_openai_compatible_sse_text(raw_text, usage_sink)
                if text:
                    write_model_trace("model_response", {
                        "trace_id": trace_id,
                        "provider": prov_name,
                        "provider_format": "openai_compatible_http",
                        "model": model,
                        "stream": False,
                        "response": text,
                        "usage": usage_sink[0] if usage_sink else None,
                    })
                    return text, None
                return None, raw_text[:2000] or "对方暂时没反应，用户稍后再试试？"

            data = resp.json()
            record_token_usage(usage_sink, data.get("usage"))
            text = ModelClient._extract_openai_compatible_text(data)
            if text:
                write_model_trace("model_response", {
                    "trace_id": trace_id,
                    "provider": prov_name,
                    "provider_format": "openai_compatible_http",
                    "model": model,
                    "stream": False,
                    "response": text,
                    "usage": usage_sink[0] if usage_sink else None,
                })
                return text, None
            return None, json.dumps(data, ensure_ascii=False)[:2000] or "对方暂时没反应，用户稍后再试试？"
        except httpx.ReadTimeout:
            return None, "网络超时了，用户稍后再试试"
        except Exception as e:
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": "openai_compatible_http",
                "model": model,
                "stream": False,
                "error": format_provider_exception(e),
            })
            return None, format_provider_exception(e)

    @staticmethod
    async def _stream_openai_compatible_http(api_key: str, base_url: str, model: str,
                                            system_prompt: str, history: list,
                                            max_tokens: Optional[int] = None,
                                            usage_sink: Optional[List[Dict[str, int]]] = None,
                                            trace_id: Optional[str] = None,
                                            prov_name: str = ""):
        import httpx
        url = f"{base_url.rstrip('/')}/chat/completions"
        body: Dict[str, Any] = {
            "model": model,
            "messages": ModelClient._build_openai_messages(system_prompt, history),
            "temperature": 0.7,
            "stream": True,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        write_model_trace("model_request", {
            "trace_id": trace_id,
            "provider": prov_name,
            "provider_format": "openai_compatible_http",
            "model": model,
            "stream": True,
            "request": body,
        })

        yielded_any_text = False
        try:
            async with httpx.AsyncClient(timeout=ModelClient._build_stream_timeout(), follow_redirects=True) as client:
                async with client.stream(
                    "POST",
                    url,
                    json=body,
                    headers=ModelClient._openai_compatible_headers(api_key, "text/event-stream"),
                ) as resp:
                    if resp.status_code >= 400:
                        error_body = await resp.aread()
                        error_text = redact_sensitive_text(error_body.decode(errors='replace'))
                        write_model_trace("model_error", {
                            "trace_id": trace_id,
                            "provider": prov_name,
                            "provider_format": "openai_compatible_http",
                            "model": model,
                            "stream": True,
                            "status_code": resp.status_code,
                            "error": error_text,
                        })
                        yield f"OpenAI compatible API error ({resp.status_code}): {error_text}"
                        return

                    async for payload in ModelClient._iter_sse_payloads(resp):
                        if payload == '[DONE]':
                            break
                        try:
                            data = json.loads(payload)
                        except json.JSONDecodeError:
                            logger.debug(f"OpenAI compatible SSE parse failed: {payload[:200]}")
                            continue
                        record_token_usage(usage_sink, data.get("usage"))
                        text = ModelClient._extract_openai_compatible_text(data)
                        if text:
                            yielded_any_text = True
                            yield text

            if not yielded_any_text:
                text, error = await ModelClient._complete_openai_compatible_http(
                    api_key, base_url, model, system_prompt, history, max_tokens, usage_sink, trace_id, prov_name
                )
                if text:
                    yield text
                elif error:
                    yield error
        except httpx.ReadTimeout:
            yield "网络超时了，用户稍后再试试"
        except Exception as e:
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": "openai_compatible_http",
                "model": model,
                "stream": True,
                "error": format_provider_exception(e),
            })
            yield format_provider_exception(e)

    @staticmethod
    async def _iter_sse_payloads(resp: Any):
        event_lines: List[str] = []

        async for line in resp.aiter_lines():
            if line == "":
                if event_lines:
                    payload_lines = []
                    for event_line in event_lines:
                        if event_line.startswith('data:'):
                            payload_lines.append(event_line[5:].lstrip())
                    event_lines.clear()
                    if payload_lines:
                        yield "\n".join(payload_lines)
                continue

            if line.startswith(':'):
                continue

            if line.startswith('data:'):
                event_lines.append(line)

        if event_lines:
            payload_lines = []
            for event_line in event_lines:
                if event_line.startswith('data:'):
                    payload_lines.append(event_line[5:].lstrip())
            if payload_lines:
                yield "\n".join(payload_lines)

    @staticmethod
    async def _complete_gemini(api_key: str, base_url: str, model: str,
                               system_prompt: str, history: list,
                               max_tokens: Optional[int] = None,
                               usage_sink: Optional[List[Dict[str, int]]] = None,
                               trace_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        import httpx

        url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
        headers = {**PROVIDER_HTTP_HEADERS, "Accept": "application/json", "x-goog-api-key": api_key}
        try:
            async with httpx.AsyncClient(timeout=ModelClient._build_stream_timeout()) as client:
                generation_config = {"temperature": 0.7}
                if max_tokens is not None:
                    generation_config["maxOutputTokens"] = max_tokens
                body = {
                    "contents": ModelClient._build_gemini_contents(history),
                    "generationConfig": generation_config,
                    "safetySettings": [
                        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
                    ]
                }
                if system_prompt:
                    body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
                write_model_trace("model_request", {
                    "trace_id": trace_id,
                    "provider_format": "gemini",
                    "model": model,
                    "stream": False,
                    "request_body": body,
                })
                resp = await client.post(url, json=body, headers=headers)

                if resp.status_code != 200:
                    error_text = redact_sensitive_text(resp.text or '')
                    write_model_trace("model_error", {
                        "trace_id": trace_id,
                        "provider_format": "gemini",
                        "model": model,
                        "status_code": resp.status_code,
                        "error": error_text,
                    })
                    return None, f"Gemini API error ({resp.status_code}): {error_text}"

                data = resp.json()
                record_token_usage(usage_sink, data.get('usageMetadata'))
                candidates = data.get('candidates', [])
                finish_reason = candidates[0].get('finishReason') if candidates else None
                text = ModelClient._extract_gemini_text_response(data)
                logger.info(
                    f"Gemini non-stream completed: model={model}, "
                    f"finishReason={finish_reason}, text_len={len(text or '')}, "
                    f"candidates={len(candidates)}, max_tokens_param={max_tokens}"
                )

                if text:
                    write_model_trace("model_response", {
                        "trace_id": trace_id,
                        "provider_format": "gemini",
                        "model": model,
                        "stream": False,
                        "response": text,
                        "usage": usage_sink[0] if usage_sink else None,
                    })
                    return text, None

                logger.warning(
                    f"Gemini non-stream returned no text; finishReason={finish_reason}, keys={list(data.keys())[:5]}"
                )
                if finish_reason and finish_reason != 'STOP':
                    return None, json.dumps(data, ensure_ascii=False)
                return None, "对方暂时没反应，用户稍后再试试？"
        except httpx.ReadTimeout as e:
            logger.error(f"Gemini Non-Stream Read Timeout: {e}")
            return None, "网络超时了，用户稍后再试试"
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Gemini Non-Stream Error: {err_msg}")
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider_format": "gemini",
                "model": model,
                "error": format_provider_exception(e),
            })
            return None, format_provider_exception(e)

    @staticmethod
    async def _complete_claude(api_key: str, base_url: str, model: str,
                               system_prompt: str, history: list,
                               max_tokens: Optional[int] = None,
                               usage_sink: Optional[List[Dict[str, int]]] = None,
                               trace_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        import httpx

        url = f"{base_url.rstrip('/')}/messages"
        headers = {
            **PROVIDER_HTTP_HEADERS,
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "application/json"
        }
        try:
            async with httpx.AsyncClient(timeout=ModelClient._build_stream_timeout()) as client:
                body = {
                    "model": model,
                    "system": system_prompt,
                    "messages": ModelClient._build_claude_messages(history),
                    "max_tokens": max_tokens if max_tokens is not None else 4096,
                }
                write_model_trace("model_request", {
                    "trace_id": trace_id,
                    "provider_format": "claude",
                    "model": model,
                    "stream": False,
                    "request_body": body,
                })
                resp = await client.post(url, json=body, headers=headers)

                if resp.status_code != 200:
                    error_text = redact_sensitive_text(resp.text or '')
                    write_model_trace("model_error", {
                        "trace_id": trace_id,
                        "provider_format": "claude",
                        "model": model,
                        "status_code": resp.status_code,
                        "error": error_text,
                    })
                    return None, f"Claude API error ({resp.status_code}): {error_text}"

                data = resp.json()
                record_token_usage(usage_sink, data.get('usage'))
                stop_reason = data.get('stop_reason')
                text = ModelClient._extract_claude_text_response(data)
                logger.info(
                    f"Claude non-stream completed: model={model}, "
                    f"stop_reason={stop_reason}, text_len={len(text or '')}, "
                    f"max_tokens_param={max_tokens}"
                )

                if text:
                    write_model_trace("model_response", {
                        "trace_id": trace_id,
                        "provider_format": "claude",
                        "model": model,
                        "stream": False,
                        "response": text,
                        "usage": usage_sink[0] if usage_sink else None,
                    })
                    return text, None

                logger.warning(
                    f"Claude non-stream returned no text; stop_reason={stop_reason}, keys={list(data.keys())[:5]}"
                )
                if stop_reason:
                    return None, json.dumps(data, ensure_ascii=False)
                return None, "对方暂时没反应，用户稍后再试试？"
        except httpx.ReadTimeout as e:
            logger.error(f"Claude Non-Stream Read Timeout: {e}")
            return None, "网络超时了，用户稍后再试试"
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Claude Non-Stream Error: {err_msg}")
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider_format": "claude",
                "model": model,
                "error": format_provider_exception(e),
            })
            return None, format_provider_exception(e)

    @staticmethod
    async def fetch_knowledge(prov_name: str, api_key: str, base_url: str, api_format: str = 'openai') -> list:
        """获取可用模型列表；兼容旧调用方，仅返回模型数组。"""
        models, _ = await ModelClient.fetch_knowledge_detailed(
            prov_name, api_key, base_url, api_format=api_format
        )
        return models

    @staticmethod
    async def fetch_knowledge_detailed(prov_name: str, api_key: str, base_url: str,
                                       api_format: str = 'openai') -> Tuple[list, Optional[str]]:
        """获取模型列表，同时返回适合展示给用户的失败原因。"""
        selected_key = get_next_api_key(prov_name, api_key)
        if not selected_key:
            return [], "没有可用的 API Key。"

        try:
            if api_format in {'gemini', 'vertex'}:
                return await ModelClient._fetch_gemini_models(selected_key, base_url), None
            if api_format == 'claude':
                return await ModelClient._fetch_claude_models(), None
            if api_format == 'openai_compatible':
                models = await ModelClient._fetch_openai_compatible_models(selected_key, base_url)
                return models, None

            client = PortalManager.get_portal(prov_name, selected_key, base_url)
            response = await client.models.list()
            model_ids = []
            for m in response.data:
                if hasattr(m, 'id'):
                    model_ids.append(m.id)
                elif isinstance(m, dict) and 'id' in m:
                    model_ids.append(m['id'])
                else:
                    model_ids.append(str(m))
            models = sorted([
                m for m in model_ids
                if 'embedding' not in m.lower() and 'audio' not in m.lower()
            ])
            return models, None
        except Exception as e:
            error_text = format_provider_exception(e)
            logger.error(f"Fetch Error ({prov_name}/{api_format}): {error_text}")
            return [], error_text

    @staticmethod
    def _format_gemini_models_error(status_code: int, response_text: str) -> str:
        """把 Gemini models.list 错误转换为不泄露 Key 的可操作提示。"""
        redacted = redact_sensitive_text(response_text or '')
        message = ''
        reason = ''
        try:
            payload = json.loads(response_text or '{}')
            error = payload.get('error') or {}
            message = str(error.get('message') or '').strip()
            for detail in error.get('details') or []:
                if isinstance(detail, dict) and detail.get('reason'):
                    reason = str(detail['reason']).strip()
                    break
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

        if status_code == 401 and reason == 'ACCESS_TOKEN_TYPE_UNSUPPORTED':
            return (
                "Google Gemini 鉴权失败（401 / ACCESS_TOKEN_TYPE_UNSUPPORTED）。"
                "这不是“没有模型”。该错误也会在 Key 被多出反斜杠、反引号或其他格式字符时出现。"
                "旧版本读取配置输入时使用了 Telegram Markdown 序列化，可能改写包含连字符或其他特殊字符的 Key。"
                "请更新并重启机器人，然后在“编辑 Key”中重新粘贴原始 Key。"
            )

        detail = message or redacted[:800] or '响应正文为空'
        suffix = f" / {reason}" if reason else ''
        return f"Gemini 模型列表请求失败（HTTP {status_code}{suffix}）：{detail}"

    @staticmethod
    async def _fetch_gemini_models(api_key: str, base_url: str) -> list:
        """获取 Google 原生 Gemini 模型列表，并处理分页及真实鉴权错误。"""
        import httpx

        url = f"{base_url.rstrip('/')}/models"
        headers = {**PROVIDER_HTTP_HEADERS, "Accept": "application/json", "x-goog-api-key": api_key}
        models: List[str] = []
        page_token: Optional[str] = None

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            while True:
                params: Dict[str, Any] = {"pageSize": 1000}
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(url, headers=headers, params=params)
                if resp.status_code != 200:
                    error = ModelClient._format_gemini_models_error(resp.status_code, resp.text or '')
                    logger.error(error)
                    raise RuntimeError(error)

                data = resp.json()
                for model_data in data.get('models', []):
                    if not isinstance(model_data, dict):
                        continue
                    supported_methods = model_data.get('supportedGenerationMethods') or []
                    if supported_methods and not any(
                        method in supported_methods
                        for method in ('generateContent', 'streamGenerateContent')
                    ):
                        continue
                    name = str(model_data.get('name') or '')
                    # name 格式: "models/gemini-2.5-flash" → 取 "gemini-2.5-flash"
                    if '/' in name:
                        name = name.split('/')[-1]
                    if name and 'embedding' not in name.lower():
                        models.append(name)

                page_token = data.get('nextPageToken')
                if not page_token:
                    break

        return sorted(set(models))

    @staticmethod
    async def _fetch_claude_models() -> list:
        """返回 Claude 常用模型列表（Anthropic 不提供 list API）"""
        return [
            'claude-sonnet-4-20250514',
            'claude-opus-4-20250514',
            'claude-3-7-sonnet-20250219',
            'claude-3-5-sonnet-20241022',
            'claude-3-5-haiku-20241022',
            'claude-3-opus-20240229',
            'claude-3-haiku-20240307',
        ]

    @staticmethod
    async def think_and_reply_stream(prov_name: str, api_key: str, base_url: str,
                                      model: str, system_prompt: str, history: list,
                                      max_tokens: Optional[int] = None, api_format: str = 'openai',
                                      usage_sink: Optional[List[Dict[str, int]]] = None,
                                      trace_id: Optional[str] = None):
        """流式回复生成器 - 支持多种 API 格式"""
        if api_format in {'gemini', 'vertex'}:
            async for chunk in ModelClient._stream_gemini(api_key, base_url, model, system_prompt, history, max_tokens, usage_sink, trace_id):
                yield chunk
            return
        elif api_format == 'claude':
            async for chunk in ModelClient._stream_claude(api_key, base_url, model, system_prompt, history, max_tokens, usage_sink, trace_id):
                yield chunk
            return
        elif api_format == 'openai_compatible':
            async for chunk in ModelClient._stream_openai_compatible_http(api_key, base_url, model, system_prompt, history, max_tokens, usage_sink, trace_id, prov_name):
                yield chunk
            return
        
        # OpenAI 兼容格式
        client = PortalManager.get_portal(prov_name, api_key, base_url)
        messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
        for msg in ModelClient.clean_memories(history):
            messages.append({
                "role": msg['role'],
                "content": ModelClient._to_openai_content(msg['content'])
            })
        
        try:
            request_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": 0.7,
                "stream": True,
            }
            if max_tokens is not None:
                request_kwargs["max_tokens"] = max_tokens
            write_model_trace("model_request", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": api_format,
                "model": model,
                "stream": True,
                "request": request_kwargs,
                "stream_options": {"include_usage": True},
            })
            try:
                stream = await ModelClient._chat_completions_api(client).create(
                    **request_kwargs,
                    stream_options={"include_usage": True}
                )
            except Exception as e:
                err_text = str(e).lower()
                if "stream_options" not in err_text and "include_usage" not in err_text:
                    raise
                logger.info(f"Provider {prov_name} does not support stream usage metadata; retrying without it")
                stream = await ModelClient._chat_completions_api(client).create(**request_kwargs)
            
            async for chunk in stream:
                record_token_usage(usage_sink, _value_from_obj(chunk, 'usage'))
                if chunk.choices and chunk.choices[0].delta.content:
                    chunk_text = ModelClient._model_content_to_text(chunk.choices[0].delta.content)
                    if chunk_text:
                        yield chunk_text
                    
        except Exception as e:
            err_msg = str(e)
            logger.error(f"Stream Think Error: {err_msg}")
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": api_format,
                "model": model,
                "stream": True,
                "error": format_provider_exception(e),
            })
            yield format_provider_exception(e)
    
    @staticmethod
    async def _stream_gemini(api_key: str, base_url: str, model: str,
                              system_prompt: str, history: list, max_tokens: Optional[int] = None,
                              usage_sink: Optional[List[Dict[str, int]]] = None,
                              trace_id: Optional[str] = None):
        """Google 原生 Gemini 流式回复（Gemini / Vertex 通用）"""
        import httpx
        
        # 构建 Gemini 格式消息
        contents = []
        for msg in ModelClient.clean_memories(history):
            role = 'model' if msg['role'] == 'assistant' else 'user'
            parts = ModelClient._to_gemini_parts(msg['content'])
            if parts:
                contents.append({"role": role, "parts": parts})
        
        url = f"{base_url.rstrip('/')}/models/{model}:streamGenerateContent?alt=sse"
        headers = {**PROVIDER_HTTP_HEADERS, "Accept": "text/event-stream", "x-goog-api-key": api_key}
        body = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.7
            },
            "safetySettings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
            ]
        }
        if system_prompt:
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        if max_tokens is not None:
            body["generationConfig"]["maxOutputTokens"] = max_tokens
        write_model_trace("model_request", {
            "trace_id": trace_id,
            "provider_format": "gemini",
            "model": model,
            "stream": True,
            "request_body": body,
        })
        
        try:
            event_count = 0
            text_event_count = 0
            last_payload_time = time.monotonic()
            async with httpx.AsyncClient(timeout=ModelClient._build_stream_timeout()) as client:
                async with client.stream('POST', url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        error_body = await resp.aread()
                        error_text = redact_sensitive_text(error_body.decode(errors='replace'))
                        write_model_trace("model_error", {
                            "trace_id": trace_id,
                            "provider_format": "gemini",
                            "model": model,
                            "stream": True,
                            "status_code": resp.status_code,
                            "error": error_text,
                        })
                        yield f"Gemini API error ({resp.status_code}): {error_text}"
                        return
                    
                    async for payload in ModelClient._iter_sse_payloads(resp):
                        now = time.monotonic()
                        gap = now - last_payload_time
                        last_payload_time = now
                        event_count += 1
                        if gap >= 1.5:
                            logger.warning(f"Gemini SSE gap {gap:.2f}s before event #{event_count}")

                        if payload == '[DONE]':
                            logger.info(f"Gemini stream done after {event_count} events, {text_event_count} text events")
                            break

                        try:
                            data = json.loads(payload)
                        except json.JSONDecodeError:
                            logger.debug(f"Gemini SSE parse failed: {payload[:200]}")
                            continue

                        record_token_usage(usage_sink, data.get('usageMetadata'))

                        candidates = data.get('candidates', [])
                        if candidates:
                            parts = candidates[0].get('content', {}).get('parts', [])
                            yielded_any_text = False
                            for part in parts:
                                text = ModelClient._model_content_to_text(part)
                                if text:
                                    yielded_any_text = True
                                    text_event_count += 1
                                    yield text
                            if not yielded_any_text:
                                finish_reason = candidates[0].get('finishReason')
                                part_shapes = [sorted(part.keys()) for part in parts if isinstance(part, dict)]
                                logger.warning(
                                    f"Gemini event #{event_count} had no text parts; finishReason={finish_reason}, part_keys={part_shapes[:3]}"
                                )
                        else:
                            logger.warning(f"Gemini event #{event_count} had no candidates: {payload[:200]}")
        except httpx.ReadTimeout as e:
            logger.error(f"Gemini Stream Read Timeout: {e}")
            yield "📖 Gemini 流式连接超时，像是线路在回复途中被中断了，请稍后再试试"
        except Exception as e:
            logger.error(f"Gemini Stream Error: {e}")
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider_format": "gemini",
                "model": model,
                "stream": True,
                "error": format_provider_exception(e),
            })
            yield f"Gemini 连接失败: {str(e)[:150]}"
    
    @staticmethod
    async def _stream_claude(api_key: str, base_url: str, model: str,
                              system_prompt: str, history: list, max_tokens: Optional[int] = None,
                              usage_sink: Optional[List[Dict[str, int]]] = None,
                              trace_id: Optional[str] = None):
        """Claude (Anthropic) 流式回复"""
        import httpx
        
        messages = []
        for msg in ModelClient.clean_memories(history):
            # Claude 消息仅支持 user/assistant；系统旁白（[系统操作] 前缀）降级为 user 保留内容
            role = 'user' if msg['role'] == 'system' else msg['role']
            messages.append({
                "role": role,
                "content": ModelClient._to_claude_content(msg['content'])
            })

        url = f"{base_url.rstrip('/')}/messages"
        headers = {
            **PROVIDER_HTTP_HEADERS,
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        headers["accept"] = "text/event-stream"
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens if max_tokens is not None else 4096,
        }
        if system_prompt:
            body["system"] = system_prompt
        write_model_trace("model_request", {
            "trace_id": trace_id,
            "provider_format": "claude",
            "model": model,
            "stream": True,
            "request_body": body,
        })
        
        try:
            async with httpx.AsyncClient(timeout=ModelClient._build_stream_timeout()) as client:
                async with client.stream('POST', url, json=body, headers=headers) as resp:
                    if resp.status_code != 200:
                        error_body = await resp.aread()
                        error_text = redact_sensitive_text(error_body.decode(errors='replace'))
                        write_model_trace("model_error", {
                            "trace_id": trace_id,
                            "provider_format": "claude",
                            "model": model,
                            "stream": True,
                            "status_code": resp.status_code,
                            "error": error_text,
                        })
                        yield f"Claude API error ({resp.status_code}): {error_text}"
                        return
                    
                    async for payload in ModelClient._iter_sse_payloads(resp):
                        if payload == '[DONE]':
                            break

                        try:
                            data = json.loads(payload)
                        except json.JSONDecodeError:
                            logger.debug(f"Claude SSE parse failed: {payload[:200]}")
                            continue

                        if data.get('type') == 'message_start':
                            message = data.get('message') or {}
                            record_token_usage(usage_sink, message.get('usage'))
                        record_token_usage(usage_sink, data.get('usage'))

                        if data.get('type') == 'content_block_delta':
                            text = data.get('delta', {}).get('text', '')
                            if text:
                                yield text
        except httpx.ReadTimeout as e:
            logger.error(f"Claude Stream Read Timeout: {e}")
            yield "📖 Claude 流式连接超时，线路可能在回复途中被打断了，请稍后再试试"
        except Exception as e:
            logger.error(f"Claude Stream Error: {e}")
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider_format": "claude",
                "model": model,
                "stream": True,
                "error": format_provider_exception(e),
            })
            yield f"Claude 连接失败: {str(e)[:150]}"

    @staticmethod
    async def think_and_reply(prov_name: str, api_key: str, base_url: str,
                              model: str, system_prompt: str, history: list,
                              max_tokens: Optional[int] = None,
                              api_format: str = 'openai',
                              usage_sink: Optional[List[Dict[str, int]]] = None,
                              trace_id: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """非流式回复（备用）"""
        if api_format in {'gemini', 'vertex'}:
            return await ModelClient._complete_gemini(
                api_key, base_url, model, system_prompt, history, max_tokens, usage_sink, trace_id
            )
        if api_format == 'claude':
            return await ModelClient._complete_claude(
                api_key, base_url, model, system_prompt, history, max_tokens, usage_sink, trace_id
            )
        if api_format == 'openai_compatible':
            return await ModelClient._complete_openai_compatible_http(
                api_key, base_url, model, system_prompt, history, max_tokens, usage_sink, trace_id, prov_name
            )

        client = PortalManager.get_portal(prov_name, api_key, base_url)
        messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
        for msg in ModelClient.clean_memories(history):
            messages.append({
                "role": msg['role'],
                "content": ModelClient._to_openai_content(msg['content'])
            })
        
        try:
            started_at = time.monotonic()
            request_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": 0.7,
            }
            if max_tokens is not None:
                request_kwargs["max_tokens"] = max_tokens
            write_model_trace("model_request", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": api_format,
                "model": model,
                "stream": False,
                "request": request_kwargs,
            })
            completion = await ModelClient._chat_completions_api(client).create(**request_kwargs)
            if not completion or not completion.choices:
                return None, "对方暂时没反应，用户稍后再试试？"
            record_token_usage(usage_sink, _value_from_obj(completion, 'usage'))

            choice = completion.choices[0]
            content = ModelClient._model_content_to_text(choice.message.content)
            if not content:
                try:
                    message_dump = choice.message.model_dump()
                except Exception:
                    message_dump = {}
                content = ModelClient._model_content_to_text(message_dump)
            if not content:
                return None, "对方暂时没反应，用户稍后再试试？"

            finish_reason = getattr(choice, 'finish_reason', None)
            logger.info(
                f"OpenAI non-stream completed: provider={prov_name}, model={model}, "
                f"finish_reason={finish_reason}, text_len={len(content)}, "
                f"elapsed={time.monotonic() - started_at:.2f}s, "
                f"max_tokens_param={max_tokens}"
            )
            write_model_trace("model_response", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": api_format,
                "model": model,
                "stream": False,
                "finish_reason": finish_reason,
                "elapsed_seconds": time.monotonic() - started_at,
                "response": content,
                "usage": usage_sink[0] if usage_sink else None,
            })
            return content, None
        except Exception as e:
            err_msg = str(e)
            logger.error(
                f"Think Error: provider={prov_name}, model={model}, "
                f"api_format={api_format}, error_type={type(e).__name__}, error={err_msg}"
            )
            write_model_trace("model_error", {
                "trace_id": trace_id,
                "provider": prov_name,
                "provider_format": api_format,
                "model": model,
                "stream": False,
                "error": format_provider_exception(e),
            })
            return None, format_provider_exception(e)

# --- ☆ 全局消息记录器 ☆ ---
