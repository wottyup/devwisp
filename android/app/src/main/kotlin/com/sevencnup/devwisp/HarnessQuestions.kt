package com.sevencnup.devwisp

import org.json.JSONArray
import org.json.JSONObject

internal data class HarnessQuestionKey(val threadId: String, val questionId: String)
internal data class HarnessQuestionRequest(val key: HarnessQuestionKey, val json: String) {
    fun questions(): JSONArray = JSONObject(json).getJSONArray("questions")

    fun reply(response: JSONObject, createdAt: String): JSONObject? {
        if (response.optBoolean("cancelled")) return null
        val answers = response.optJSONArray("answers") ?: return null
        val byId = (0 until answers.length()).associate { index ->
            val answer = answers.getJSONObject(index)
            answer.getString("id") to answer
        }
        val questions = questions()
        val text = (0 until questions.length()).map { index ->
            val question = questions.getJSONObject(index)
            val answer = byId[question.getString("id")] ?: return null
            val selected = answer.optJSONArray("selected") ?: JSONArray()
            val values = (0 until selected.length()).map { selected.getString(it) } +
                listOfNotNull(answer.optString("custom").trim().takeIf { it.isNotBlank() })
            "${index + 1}. ${question.optString("question")}\n答：${values.joinToString("、")}"
        }.joinToString("\n\n")
        val turnId = JSONObject(json).optString("turnId").takeIf { it.isNotBlank() } ?: return null
        return JSONObject().put("id", "user-question-${key.questionId}").put("type", "userMessage")
            .put("status", "completed").put("createdAt", createdAt).put("turnId", turnId)
            .apply { if (JSONObject(json).has("afterItemId")) put("afterItemId", JSONObject(json).optString("afterItemId")) }
            .put("content", JSONArray().put(JSONObject().put("type", "text").put("text", text)))
    }

    companion object {
        fun fromJson(value: JSONObject): HarnessQuestionRequest? {
            val thread = (value.opt("threadId") as? String)?.takeIf { it.isNotBlank() } ?: return null
            val id = (value.opt("questionId") as? String)?.takeIf { it.isNotBlank() } ?: return null
            val questions = value.optJSONArray("questions") ?: return null
            if (questions.length() == 0) return null
            val ids = (0 until questions.length()).map { (questions.optJSONObject(it)?.opt("id") as? String).orEmpty() }
            if (ids.any { it.isBlank() } || ids.distinct().size != ids.size) return null
            return HarnessQuestionRequest(HarnessQuestionKey(thread, id), value.toString())
        }
    }
}

/** Revisions prevent a stale HTTP snapshot from resurrecting resolved socket requests. */
internal class HarnessQuestionInbox {
    private val requests = linkedMapOf<HarnessQuestionKey, HarnessQuestionRequest>()
    private val submitting = mutableSetOf<HarnessQuestionKey>()
    private val drafts = mutableMapOf<HarnessQuestionKey, MutableMap<String, JSONObject>>()
    var revision = 0L
        private set

    fun forThread(threadId: String?) = requests.values.filter { it.key.threadId == threadId }
    fun isSubmitting(key: HarnessQuestionKey) = key in submitting
    fun request(key: HarnessQuestionKey) = requests[key]
    fun answer(key: HarnessQuestionKey, id: String): JSONObject = drafts.getOrPut(key) { mutableMapOf() }
        .getOrPut(id) { JSONObject().put("id", id).put("selected", JSONArray()) }

    fun receive(request: HarnessQuestionRequest): Boolean {
        revision++
        return requests.put(request.key, request) != request
    }

    fun resolve(key: HarnessQuestionKey): Boolean {
        revision++
        submitting.remove(key)
        drafts.remove(key)
        return requests.remove(key) != null
    }

    fun reconcile(snapshot: List<HarnessQuestionRequest>, expectedRevision: Long) {
        if (revision != expectedRevision) return
        val replacement = snapshot.associateByTo(linkedMapOf()) { it.key }
        if (requests == replacement) return
        requests.clear()
        requests.putAll(replacement)
        submitting.retainAll(requests.keys)
        drafts.keys.retainAll(requests.keys)
        revision++
    }

    fun response(key: HarnessQuestionKey): JSONObject? {
        val request = requests[key] ?: return null
        val questions = request.questions()
        val answers = JSONArray()
        for (i in 0 until questions.length()) {
            val question = questions.getJSONObject(i)
            val answer = answer(key, question.getString("id"))
            val selected = answer.optJSONArray("selected") ?: JSONArray()
            val custom = answer.optString("custom").trim()
            val options = question.optJSONArray("options") ?: JSONArray()
            val allowed = (0 until options.length()).map { options.getJSONObject(it).getString("label") }.toSet()
            val values = (0 until selected.length()).map { selected.optString(it) }
            if (values.any { it !in allowed } || values.distinct().size != values.size || custom.length > 30_000 ||
                values.isEmpty() && custom.isEmpty() ||
                !question.optBoolean("multiSelect") && values.size + (if (custom.isNotEmpty()) 1 else 0) > 1 ||
                question.optJSONObject("intent")?.optString("kind") == "approval" && custom.isNotEmpty()) return null
            answers.put(JSONObject().put("id", question.getString("id")).put("selected", JSONArray(values))
                .apply { if (custom.isNotEmpty()) put("custom", custom) })
        }
        return JSONObject().put("answers", answers)
    }

    fun beginDecision(key: HarnessQuestionKey): Boolean {
        if (key !in requests || !submitting.add(key)) return false
        revision++
        return true
    }

    fun finishDecision(key: HarnessQuestionKey, resolved: Boolean) {
        if (resolved) resolve(key) else { submitting.remove(key); revision++ }
    }

    fun clear() { requests.clear(); submitting.clear(); drafts.clear(); revision++ }
}

/** Confirmed replies and captured boundaries for servers that combine assistant output. */
internal object HarnessQuestionReplies {
    fun add(saved: JSONArray, reply: JSONObject): JSONArray = JSONArray().apply {
        for (index in 0 until saved.length()) {
            val item = saved.optJSONObject(index) ?: continue
            if (item.optString("id") != reply.optString("id")) put(item)
        }
        put(reply)
    }

    private fun text(item: JSONObject): String = item.optString("text").ifEmpty {
        val content = item.optJSONArray("content") ?: return@ifEmpty ""
        (0 until content.length()).joinToString("") { content.optJSONObject(it)?.optString("text").orEmpty() }
    }

    fun merge(turns: JSONArray, saved: JSONArray): Pair<JSONArray, JSONArray> {
        if (saved.length() == 0) return turns to saved
        val result = JSONArray(turns.toString())
        val remaining = linkedMapOf<String, JSONObject>()
        for (index in 0 until saved.length()) saved.optJSONObject(index)?.let { remaining[it.optString("id")] = it }
        for (turnIndex in 0 until result.length()) {
            val turn = result.optJSONObject(turnIndex) ?: continue
            val original = turn.optJSONArray("items") ?: continue
            val replies = remaining.values.filter { it.optString("turnId") == turn.optString("id") }
            val originals = (0 until original.length()).mapNotNull { original.optJSONObject(it) }
            val byId = originals.associateBy { it.optString("id") }
            val moved = mutableSetOf<String>()
            val replacements = mutableMapOf<String, List<JSONObject>>()
            val boundaryGroups = replies.filter { it.optJSONObject("_assistantBoundary") != null }
                .groupBy { it.getJSONObject("_assistantBoundary").optString("itemId") }
            boundaryGroups.forEach { (itemId, candidates) ->
                val source = byId[itemId] ?: return@forEach
                if (!source.optString("type").equals("agentMessage", true)) return@forEach
                val full = text(source)
                val sourceIndex = originals.indexOf(source)
                val valid = candidates.filter { reply ->
                    val prefix = reply.getJSONObject("_assistantBoundary").optString("text")
                    val echo = byId[reply.optString("id")]
                    val alreadyOrdered = turn.optString("status") == "completed" && echo != null &&
                        originals.indexOf(echo) > sourceIndex && full == prefix
                    if (alreadyOrdered) remaining.remove(reply.optString("id"))
                    !alreadyOrdered && full.startsWith(prefix)
                }.sortedBy { it.getJSONObject("_assistantBoundary").optString("text").length }
                if (valid.isEmpty()) return@forEach
                val pieces = mutableListOf<JSONObject>()
                var offset = 0
                fun segment(end: Int, id: String, completed: Boolean) {
                    val raw = full.substring(offset, end)
                    val content = if (offset == 0) raw else raw.trimStart('\r', '\n')
                    if (content.isNotEmpty()) {
                        val item = JSONObject(source.toString()).put("id", id).put("text", content)
                        item.remove("content")
                        if (completed) {
                            item.put("status", "completed")
                            item.remove("_liveReplyItemId")
                            item.remove("_liveReplyPrefixLength")
                        } else item.put("_liveReplyPrefixLength", offset + raw.length - content.length)
                        pieces += item
                    }
                    offset = end
                }
                valid.forEach { reply ->
                    val id = reply.getString("id")
                    val end = reply.getJSONObject("_assistantBoundary").getString("text").length
                    segment(end, "$itemId-before-$id", true)
                    pieces += byId[id] ?: reply
                    moved += id
                }
                segment(full.length, itemId, false)
                replacements[itemId] = pieces
            }
            val items = originals.flatMap { item ->
                val id = item.optString("id")
                if (id in moved) emptyList() else replacements[id] ?: listOf(item)
            }.toMutableList()
            replies.forEach { reply ->
                val id = reply.optString("id")
                if (id in moved) return@forEach // Keep its boundary until the server stores separate segments.
                if (items.any { it.optString("id") == id }) {
                    if (reply.optJSONObject("_assistantBoundary") == null) remaining.remove(id)
                    return@forEach
                }
                val anchor = reply.optString("afterItemId")
                val anchorIndex = if (anchor.isBlank()) -1 else items.indexOfFirst { it.optString("id") == anchor }
                val createdAt = reply.optString("createdAt")
                val laterIndex = items.indexOfFirst { item ->
                    runCatching { java.time.Instant.parse(item.optString("createdAt"))
                        .isAfter(java.time.Instant.parse(createdAt)) }.getOrDefault(false)
                }
                val firstAssistant = if (reply.has("afterItemId") && anchor.isEmpty())
                    items.indexOfFirst { it.optString("type").equals("agentMessage", true) } else -1
                val insertion = when {
                    anchorIndex >= 0 -> anchorIndex + 1
                    firstAssistant >= 0 -> firstAssistant
                    laterIndex >= 0 -> laterIndex
                    else -> items.size
                }
                items.add(insertion, reply)
            }
            turn.put("items", JSONArray(items))
        }
        return result to JSONArray(remaining.values.toList())
    }
}
